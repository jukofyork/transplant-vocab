#!/usr/bin/env python3
"""
Vocab Transplantation Tool

All credit to turboderp for the original idea:

https://huggingface.co/turboderp/Qwama-0.5B-Instruct/blob/main/vocab_transplant.py
"""

import argparse
import json
import os
import shutil
import sys
from typing import Tuple, Dict

import torch
from tqdm import tqdm
from transformers import AutoTokenizer, AutoModelForCausalLM, AutoConfig

import torch.nn as nn

def parse_arguments() -> argparse.Namespace:
    """Parse and validate command line arguments"""
    parser = argparse.ArgumentParser(
        description = "Transplant token embeddings between language models",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("donor_dir", help = "Path to donor model directory")
    parser.add_argument("target_dir", help = "Path to target model directory")
    parser.add_argument("output_dir", help = "Path to output model directory")
    parser.add_argument("--override", nargs = 2, action = "append", default = [],
                       help = "Override target token with donor token (can be used multiple times)")
    parser.add_argument("--weighting-decay-factor", type = float, default = 0.5,
                       help = "Decay factor [0-1] for multi-token mappings: "
                            "0=first token only, 0.5=decreasing weights, 1=uniform mean")
    parser.add_argument("--use-cpu-only", action = "store_true",
                       help = "Use CPU only for model loading and processing in float32")
    parser.add_argument("--trust-remote-code", action = "store_true",
                       help = "Allow custom code execution when loading models with non-standard architectures")
    parser.add_argument("--overwrite", action = "store_true",
                       help = "Overwrite output directory if it exists")
    parser.add_argument("--verbose", action = "store_true",
                       help = "Show detailed token mapping output")

    args = parser.parse_args()

    if not (0.0 <= args.weighting_decay_factor <= 1.0):
        sys.exit(f"Error: --weighting-decay-factor must be between 0.0 and 1.0 (got {args.weighting_decay_factor})")

    return args

def validate_directories(args: argparse.Namespace) -> None:
    """Validate input/output directory structure and permissions"""
    for dir_type, dir_path in [("donor", args.donor_dir), ("target", args.target_dir)]:
        if not os.path.isdir(dir_path):
            sys.exit(f"Error: {dir_type} directory does not exist: {dir_path}")
        if not os.access(dir_path, os.R_OK):
            sys.exit(f"Error: No read permissions for {dir_type} directory: {dir_path}")

    if os.path.exists(args.output_dir):
        if args.overwrite:
            if not os.access(args.output_dir, os.W_OK):
                sys.exit(f"Error: No write permissions for output directory: {args.output_dir}")
            shutil.rmtree(args.output_dir)
        else:
            sys.exit(f"Error: Output directory exists (use --overwrite to replace): {args.output_dir}")

    try:
        os.makedirs(args.output_dir, exist_ok = True)
    except OSError as e:
        sys.exit(f"Error: Failed to create output directory: {e}")

def load_model_config(path: str) -> dict:
    """Load model configuration"""
    config_path = os.path.join(path, "config.json")
    if not os.path.exists(config_path):
        sys.exit(f"Error: Config file not found at {config_path}")

    try:
        print(f"Loading config from '{path}'... ", end = "")
        with open(config_path, "r", encoding = "utf-8") as f:
            config = json.load(f)
        print("Done.")
    except Exception as e:
        sys.exit(f"Error loading config from {config_path}: {e}")

    return config

def load_tokenizer(path: str, trust_remote_code = False) -> AutoTokenizer:
    """Load tokenizer with error handling"""
    try:
        print(f"Loading tokenizer from '{path}'... ", end = "")
        tokenizer = AutoTokenizer.from_pretrained(path, trust_remote_code = trust_remote_code)
        print("Done.")
        return tokenizer
    except Exception as e:
        sys.exit(f"Failed to load tokenizer: {e}")

def load_model(path: str, trust_remote_code = False, torch_dtype = None) -> AutoModelForCausalLM:
    """Load model with error handling"""
    try:
        print(f"Loading model from '{path}'... ", end = "")
        if torch_dtype is not None:
            model = AutoModelForCausalLM.from_pretrained(
                path,
                device_map = "auto",
                trust_remote_code = trust_remote_code,
                torch_dtype = torch_dtype
            )
        else:
            model = AutoModelForCausalLM.from_pretrained(
                path,
                trust_remote_code = trust_remote_code,
                device_map = "cpu"  # Will also load (and save) as torch.float32
            )
        print("Done.")
        return model
    except Exception as e:
        sys.exit(f"Failed to load model: {e}")

def compute_front_loaded_mean(v, weighting_decay_factor = 0.5):
    """
    Computes the "front-loaded" exponentially-weighted mean with a weighting decay factor.
    
    Parameters:
    - v: torch tensor with values
    - weighting_decay_factor: parameter in [0, 1] controlling how quickly weights decay for subsequent vectors
    
    Returns:
    - Weighted average tensor
    
    Special cases:
    - weighting_decay_factor=0   : Returns only the first vector (maximum front-loading)
    - weighting_decay_factor=0.5 : Applies weights 1, 0.5, 0.25, 0.125, ... (earlier vectors have more influence)
    - weighting_decay_factor=1   : Returns the uniform arithmetic mean (no front-loading)
    """
    # Assert that weighting_decay_factor is in the valid range [0, 1]
    assert 0 <= weighting_decay_factor <= 1, f"weighting_decay_factor must be in range [0, 1], got {weighting_decay_factor}"

    n = v.shape[0]

    if n == 1 or weighting_decay_factor == 0:
        return v[0]  # First (or only) vector only
    elif weighting_decay_factor == 1:
        return torch.mean(v, dim = 0)  # Arithmetic mean
    else:
        # Compute the weights using geometric progression
        decay_powers = torch.tensor([weighting_decay_factor ** i for i in range(n)], device = v.device)
        decay_powers = decay_powers.view(-1, *([1] * (v.dim() - 1)))
        weighted_sum = torch.sum(decay_powers * v, dim = 0)
        denominator = torch.sum(decay_powers)
        return weighted_sum / denominator

def main():
    args = parse_arguments()
    validate_directories(args)

    # Load configurations
    donor_config = load_model_config(args.donor_dir)
    target_config = load_model_config(args.target_dir)

    # Get the donor vocabulary size (for both flat and nested configurations)
    if "text_config" in donor_config and "vocab_size" in donor_config["text_config"]:
        donor_vocab_size = donor_config["text_config"]["vocab_size"]
    else:
        assert "vocab_size" in donor_config, "vocab_size not found in source model config"
        donor_vocab_size = donor_config["vocab_size"]

    # Get the donor hidden size (for both flat and nested configurations)
    if "text_config" in donor_config and "hidden_size" in donor_config["text_config"]:
        donor_vocab_size = donor_config["text_config"]["hidden_size"]
    else:
        assert "hidden_size" in donor_config, "hidden_size not found in source model config"
        donor_hidden_size = donor_config["hidden_size"]

    # Get the target vocabulary size (for both flat and nested configurations)
    if "text_config" in target_config and "vocab_size" in target_config["text_config"]:
        target_vocab_size = target_config["text_config"]["vocab_size"]
    else:
        assert "vocab_size" in target_config, "vocab_size not found in target model config"
        target_vocab_size = target_config["vocab_size"]

    # Load tokenizers
    donor_tokenizer = load_tokenizer(args.donor_dir, args.trust_remote_code)
    target_tokenizer = load_tokenizer(args.target_dir, args.trust_remote_code)

    # Load the donor model
    if args.use_cpu_only:
        model = load_model(args.donor_dir, args.trust_remote_code)
    else:
        model = load_model(args.donor_dir, args.trust_remote_code, donor_config.get("torch_dtype", None))

    # The config file counts the all tokens, but we also need to know how many are used for the loop
    used_target_vocab_size = max(target_tokenizer.vocab.values()) + 1
    unused_target_vocab_size = target_vocab_size - used_target_vocab_size

    print("\nLoaded OK:")
    print(f"- Donor vocab size  : {donor_vocab_size}")
    print(f"- Target vocab size : {target_vocab_size} (used = {used_target_vocab_size}, unused = {unused_target_vocab_size})")
    print(f"- Donor hidden size : {donor_hidden_size}")

    # Automatic and manual overrides
    override_map = {}

    # Process the automatic overrides
    special_tokens = ['bos_token_id', 'eos_token_id', 'pad_token_id']
    print(f"\nProcessing {len(special_tokens)} automatic token overrides:")
    for token_attr in special_tokens:
        # First try to get from the tokenizer
        target_token_id = getattr(target_tokenizer, token_attr)
        donor_token_id = getattr(donor_tokenizer, token_attr)

        # Try to get from config if not found in tokenizer
        if target_token_id is None and token_attr in target_config:
            target_token_id = target_config[token_attr]
        if donor_token_id is None and token_attr in donor_config:
            donor_token_id = donor_config[token_attr]

        # Try to perform the automatic match
        if target_token_id is not None:
            if donor_token_id is not None:
                if target_token_id not in override_map:
                    target_token = target_tokenizer.convert_ids_to_tokens(target_token_id)
                    donor_token = donor_tokenizer.convert_ids_to_tokens(donor_token_id)
                    override_map[target_token_id] = torch.tensor([donor_token_id], dtype = torch.long)
                    print(f"✔ {repr(token_attr)} : {target_token_id} {repr(target_token)} → [{donor_token_id}] {repr(donor_token)}")
                else:
                    print(f"✘ {repr(token_attr)} : {target_token_id} is already mapped to [{override_map[target_token_id].item()}]")
            else:
                print(f"✘ {repr(token_attr)} : Not found for donor model");
        else:
            print(f"✘ {repr(token_attr)} : Not found for target model");

    # Process manual token overrides
    if args.override:
        print(f"\nProcessing {len(args.override)} manual token overrides:")
        for target_token, donor_tokens in args.override:
            # Encode target token and verify it's a single token
            target_id = target_tokenizer.encode(target_token, add_special_tokens = False)
            assert len(target_id) == 1, f"Target token '{target_token}' maps to {len(target_id)} tokens. Must be a 1 token."
            target_id = target_id[0]

            # Replace newline characters with the actual byte representation of a newline (0x0A)
            # NOTE: If you don't do this then it will get wrong;y encoded as the "\\n" string literal
            if "\\n" in donor_tokens:
                donor_tokens = donor_tokens.replace("\\n", chr(10))

            # Get the IDs from the token string
            encoded = donor_tokenizer.encode(donor_tokens, add_special_tokens = False, return_tensors = "pt").flatten()
            assert encoded.numel() != 0, f"Donor token '{donor_tokens}' for target ID {idx} encodes to 0 tokens."

            # Store the donor token IDs
            override_map[target_id] = encoded

            print(f"✔ {target_id:6d} : {repr(target_token)} → {encoded.tolist()} {repr(donor_tokens)}")
    print()

    # NOTE: We need to "untie" the lm_head weights for models with tie_word_embeddings = True
    donor_embed_tokens = model.model.embed_tokens.weight
    donor_lm_head = model.model.embed_tokens.weight if donor_config.get("tie_word_embeddings", False) else model.lm_head.weight

    # Initialize new embedding and head tensors with zeros
    new_embed_tokens = torch.zeros(
        (target_vocab_size, donor_hidden_size),
        dtype = donor_embed_tokens.dtype,
        device = donor_embed_tokens.device
    )
    new_lm_head = torch.zeros(
        (target_vocab_size, donor_hidden_size),
        dtype = donor_lm_head.dtype,
        device = donor_lm_head.device
    )

    # Track mapping statistics
    mapping_counts = {}

    # Track lm_head statistics
    lm_head_copy_count = 0
    lm_head_mean_count = 0

    # Configure progress display
    iterator = range(used_target_vocab_size)
    if args.verbose:
        print("Transplanting tokens:")
    else:
        iterator = tqdm(iterator, desc = "Transplanting tokens", unit = "token")

    for idx in iterator:
        decoded = target_tokenizer.decode([idx], decode_special_tokens = True)
        if idx in override_map:
            encoded = override_map[idx]
        else:
            encoded = donor_tokenizer.encode(decoded, add_special_tokens = False, return_tensors = "pt").flatten()

        if args.verbose:
            print(f"- {idx:6d} : {repr(decoded)} → {encoded.tolist()}")

        # Track mapping types
        if encoded.numel() in mapping_counts:
            mapping_counts[encoded.numel()] += 1
        else:
            mapping_counts[encoded.numel()] = 1

        # Use only the final token of encoded sequence for input embeddings
        new_embed_tokens[idx] = donor_embed_tokens[encoded[-1]]

        # Use a "front-loaded" exponentially-weighted mean for lm_head embeddings
        if encoded.numel() == 1:
            new_lm_head[idx] = donor_lm_head[encoded[0].item()]
            lm_head_copy_count += 1
        else:
            head_embeddings = donor_lm_head[encoded.flatten()]
            new_lm_head[idx] = compute_front_loaded_mean(head_embeddings, args.weighting_decay_factor)
            lm_head_mean_count += 1

    # Print statistics
    print("\nTransplant mappings:")
    for count, occurrences in sorted(mapping_counts.items()):
        mapping_label = f"{count} to 1"
        print(f"- {mapping_label:<8}: {occurrences} ({(occurrences/used_target_vocab_size*100):.2g}%)")

    print("\nHead initialized with:")
    lm_head_zeroed_count = target_vocab_size - (lm_head_copy_count + lm_head_mean_count)
    print(f"- Copies : {lm_head_copy_count} ({(lm_head_copy_count/target_vocab_size*100):.2g}%)")
    print(f"- Means  : {lm_head_mean_count} ({(lm_head_mean_count/target_vocab_size*100):.2g}%)")
    print(f"- Zeros  : {lm_head_zeroed_count} ({(lm_head_zeroed_count/target_vocab_size*100):.2g}%)")

    # Make a copy of the model's state_dict and get the type
    new_state_dict = model.state_dict().copy()
    old_dtype = model.model.embed_tokens.weight.dtype

    # Update the state_dict with new embeddings
    new_state_dict['model.embed_tokens.weight'] = new_embed_tokens.to(dtype = old_dtype)
    new_state_dict['lm_head.weight'] = new_lm_head.to(dtype = old_dtype)

    # Update model architecture
    model.model.embed_tokens.num_embeddings = target_vocab_size
    model.lm_head.out_features = target_vocab_size

    # Update model config
    model.config.update({
        'vocab_size': target_vocab_size,
        'bos_token_id': target_tokenizer.bos_token_id,
        'eos_token_id': target_tokenizer.eos_token_id,
    })

    # Update the config's pad_token_id if it exists
    if hasattr(model.config, 'pad_token_id'):
        if target_tokenizer.pad_token_id is not None:
            model.config.update({'pad_token_id': target_tokenizer.pad_token_id})
        else:
            model.config.update({'pad_token_id': target_tokenizer.eos_token_id})  # Default to EOS if no PAD to copy

    # Set the config's tie_word_embeddings to False if it exists
    if hasattr(model.config, 'tie_word_embeddings'):
        model.config.update({'tie_word_embeddings': False})

    # Save final model and tokenizer
    print(f"\nSaving model and tokenizer to '{args.output_dir}' folder")
    model.save_pretrained(args.output_dir, state_dict = new_state_dict, safe_serialization = True)
    target_tokenizer.save_pretrained(args.output_dir)

    # Only modify if the donor tokenizer doesn't use BOS tokens
    if getattr(donor_tokenizer, "add_bos_token", False) or getattr(donor_tokenizer, "bos_token", None) is None:
        tokenizer_config_path = os.path.join(args.output_dir, "tokenizer_config.json")
        if os.path.exists(tokenizer_config_path):
            print(f"\nPatching BOS handling in '{tokenizer_config_path}'")
            try:
                # Read the file as text without specifying encoding
                with open(tokenizer_config_path, "r") as f:
                    config_text = f.read()

                # Make sure that add_bos_token is set to false
                config_text = config_text.replace('"add_bos_token": true', '"add_bos_token": false')
                print("- Updated 'add_bos_token' configuration.")

                # Remove any use of bos_token from chat template
                # NOTE: We can't (safely) set '"bos_token": null', but it shouldn't matter with these two patches...
                config_text = config_text.replace("{{ bos_token }}", "").replace("{{bos_token}}", "")
                print("- Removed all references to 'bos_token' from Jinja chat template.")

                # Write the modified text back without specifying encoding
                with open(tokenizer_config_path, "w") as f:
                    f.write(config_text)
            except Exception as e:
                print(f"Warning: Failed to patch tokenizer configuration: {e}")

    # TODO: Figure out why it causes a segmentation fault on exit???
    print("\nOperation completed successfully (ignore any 'segmentation fault' that follows!!!)")

if __name__ == "__main__":
    main()
