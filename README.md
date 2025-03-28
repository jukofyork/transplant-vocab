# Vocab Transplantation Tool

Transplants vocabulary between language models, enabling the creation of draft models for efficient speculative decoding **WITHOUT** retraining.

This tool allows you to combine the transformer architecture and weights from a donor model with the tokenizer of a target model, creating a hybrid model that can serve as a draft model in speculative decoding pipelines. By matching token-to-token or multi-token mappings between vocabularies, it intelligently transfers embeddings while preserving semantic relationships. This approach eliminates the need for expensive retraining or distillation procedures typically required for creating compatible draft models, making it an efficient solution for accelerating inference through speculative decoding techniques.

## Features

- Preserve the donor model's intelligence/performance.
- Adapt donor model to use the target model's tokenizer.
- Automatic special tokens mapping between models.
- User-specified manual token mapping overrides.

## Installation

```bash
pip install -r requirements.txt
```

**Requirements:**
- Python 3.8+
- PyTorch 2.0+
- Transformers 4.30+
- tqdm

## Usage

### Basic Command
```bash
python transplant_vocab.py /path/to/donor_model /path/to/target_model /path/to/output_model
```

### Options

| Flag | Description |
|------|-------------|
| `--override TARGET DONOR` | Override target token with donor token (can be used multiple times) |
| `--weighting-decay-factor [0-1]` | Decay factor for multi-token mappings: 0=first token only, 0.5=decreasing weights, 1=uniform mean |
| `--use-cpu-only` | Use CPU instead of GPU (and with `float32` precision) |
| `--trust-remote-code` | Allow custom code execution when loading models with non-standard architectures |
| `--overwrite` | Replace existing output directory |
| `--verbose` | Show detailed token mapping output |

### Examples

To transplant `DeepSeek-R1` tokenizer into `Qwen2.5-0.5B-Instruct` model and output as new model called `DeepSeek-R1-DRAFT-0.5B`:

```bash
python transplant_vocab.py ./Qwen2.5-0.5B-Instruct ./DeepSeek-R1 ./DeepSeek-R1-DRAFT-0.5B
```

With manual token mapping overrides for chat templates (see below for detailed explanation):

```bash
python transplant_vocab.py ./Qwen2.5-0.5B-Instruct ./DeepSeek-R1 ./DeepSeek-R1-DRAFT-0.5B \
	--override "<｜User｜>" "<|im_start|>user\\n" \
	--override "<｜Assistant｜>" "<|im_start|>assistant\\n" \
	--override ...
```

Use only first token for `lm_head` averaging (maximum front-loading):

```bash
python transplant_vocab.py ./Qwen2.5-0.5B-Instruct ./DeepSeek-R1 ./DeepSeek-R1-DRAFT-0.5B-first --weighting-decay-factor 0.0
```

Use uniform mean for `lm_head` averaging (ie: equal weight to all tokens):

```bash
python transplant_vocab.py ./Qwen2.5-0.5B-Instruct ./DeepSeek-R1 ./DeepSeek-R1-DRAFT-0.5B-mean --weighting-decay-factor 1.0
```

Use decreasing weights (eg: 1, 0.5, 0.25, etc.) for `lm_head` averaging (default behaviour):

```bash
python transplant_vocab.py ./Qwen2.5-0.5B-Instruct ./DeepSeek-R1 ./DeepSeek-R1-DRAFT-0.5B-decay --weighting-decay-factor 0.5
```

### Token Mapping

#### Automatic Special Token Mapping

The tool automatically attempts to map three special tokens between models:
- `bos_token_id` (Beginning of Sequence)
- `eos_token_id` (End of Sequence)
- `pad_token_id` (Padding)

These mappings ensure that the transplanted model correctly handles sequence boundaries and padding, which is critical for proper functioning.

**NOTE**: Some models reuse `eos_token_id` as `pad_token_id` so this automatic process is not possible in these cases, eg:

```
Processing 3 automatic token overrides:
✔ 'bos_token_id' : 0 '<｜begin▁of▁sentence｜>' → [151643] '<|endoftext|>'
✔ 'eos_token_id' : 1 '<｜end▁of▁sentence｜>' → [151645] '<|im_end|>'
✘ 'pad_token_id' : 1 is already mapped to [151645]
```

**NOTE**: Some models (eg: `qwen`) don't use any `bos_token_id` so we try to manually patch `tokenizer_config.json` to fix this at the end.

#### Manual Token Mapping Overrides

For more complex models, especially those with chat templates or special tokens for specific tasks, you can manually map tokens using the `--override` option:

```bash
python transplant_vocab.py ./donor_model ./target_model ./output_model --override "<target_token>" "<donor_sequence>"
```

You can specify multiple overrides by repeating the `--override` option. This is particularly useful for:
- Chat template tokens (user/assistant markers)
- Special task tokens (FIM, tool calls, etc.)
- Any token that needs specific handling

#### Example: Mapping Chat and Special Tokens

Here's a real-world example of manually mapping `Qwen2.5` tokens to `DeepSeek-V3` tokens:

```bash
python transplant_vocab.py ./Qwen2.5-0.5B-Instruct ./DeepSeek-V3 ./DeepSeek-V3-DRAFT-0.5B \
	--override "<｜▁pad▁｜>" "<|endoftext|>" \
	--override "<｜fim▁hole｜>" "<|fim_middle|>" \
	--override "<｜fim▁begin｜>" "<|fim_prefix|>" \
	--override "<｜fim▁end｜>" "<|fim_suffix|>" \
	--override "<｜User｜>" "<|im_start|>user\n" \
	--override "<｜Assistant｜>" "<|im_start|>assistant\n" \
	--override "<|EOT|>" "<|endoftext|>" \
	--override "<｜tool▁calls▁begin｜>" "<tool_call>" \
	--override "<｜tool▁call▁begin｜>" "<tool_call>" \
	--override "<｜tool▁outputs▁begin｜>" "<tool_call>" \
	--override "<｜tool▁output▁begin｜>" "<tool_call>" \
	--override "<｜tool▁calls▁end｜>" "</tool_call>" \
	--override "<｜tool▁call▁end｜>" "</tool_call>" \
	--override "<｜tool▁outputs▁end｜>" "</tool_call>" \
	--override "<｜tool▁output▁end｜>" "</tool_call>" \
	--override "<｜tool▁sep｜>" "</tool_call>"
```

which should output something like this:

```
Processing 16 manual token overrides:
✔      2 : '<｜▁pad▁｜>' → [151643] '<|endoftext|>'
✔ 128800 : '<｜fim▁hole｜>' → [151660] '<|fim_middle|>'
✔ 128801 : '<｜fim▁begin｜>' → [151659] '<|fim_prefix|>'
✔ 128802 : '<｜fim▁end｜>' → [151661] '<|fim_suffix|>'
✔ 128803 : '<｜User｜>' → [151644, 872, 198] '<|im_start|>user\n'
✔ 128804 : '<｜Assistant｜>' → [151644, 77091, 198] '<|im_start|>assistant\n'
✔ 128805 : '<|EOT|>' → [151643] '<|endoftext|>'
✔ 128806 : '<｜tool▁calls▁begin｜>' → [151657] '<tool_call>'
✔ 128808 : '<｜tool▁call▁begin｜>' → [151657] '<tool_call>'
✔ 128810 : '<｜tool▁outputs▁begin｜>' → [151657] '<tool_call>'
✔ 128812 : '<｜tool▁output▁begin｜>' → [151657] '<tool_call>'
✔ 128807 : '<｜tool▁calls▁end｜>' → [151658] '</tool_call>'
✔ 128809 : '<｜tool▁call▁end｜>' → [151658] '</tool_call>'
✔ 128811 : '<｜tool▁outputs▁end｜>' → [151658] '</tool_call>'
✔ 128813 : '<｜tool▁output▁end｜>' → [151658] '</tool_call>'
✔ 128814 : '<｜tool▁sep｜>' → [151658] '</tool_call>'
```

**NOTE**: I suggest you use the `--verbose` flag to verify your mappings are working as expected, eg:

```
Transplanting tokens:
-      0 : '<｜begin▁of▁sentence｜>' → [151643]
-      1 : '<｜end▁of▁sentence｜>' → [151645]
-      2 : '<｜▁pad▁｜>' → [151643]
- 128800 : '<｜fim▁hole｜>' → [151660]
- 128801 : '<｜fim▁begin｜>' → [151659]
- 128802 : '<｜fim▁end｜>' → [151661]
- 128803 : '<｜User｜>' → [151644, 872, 198]
- 128804 : '<｜Assistant｜>' → [151644, 77091, 198]
- 128805 : '<|EOT|>' → [151643]
- 128806 : '<｜tool▁calls▁begin｜>' → [151657]
- 128807 : '<｜tool▁calls▁end｜>' → [151658]
- 128808 : '<｜tool▁call▁begin｜>' → [151657]
- 128809 : '<｜tool▁call▁end｜>' → [151658]
- 128810 : '<｜tool▁outputs▁begin｜>' → [151657]
- 128811 : '<｜tool▁outputs▁end｜>' → [151658]
- 128812 : '<｜tool▁output▁begin｜>' → [151657]
- 128813 : '<｜tool▁output▁end｜>' → [151658]
- 128814 : '<｜tool▁sep｜>' → [151658]
```

and also to explore other possible manual overrides...

## Design Rationale

### Input Embeddings (Final Token Strategy)
When a target token maps to multiple donor tokens:
```text
Target: [X] → Donor: [A, B, C]
```
We use **C** (**ONLY** the final token) because:

1. Transformers process tokens sequentially, with transformer blocks "looking backward".
2. It's the transformer blocks that integrate context from previous tokens.
3. Taking the mean of all tokens doesn't align with how transformers process sequences.
4. Using the final token aligns with how the transformers process the previous token to create the next token.

### Output Head (First Token Strategy)
When a target token maps to multiple donor tokens:
```text
Target: [Y] → Donor: [D, E, F]
```
We use **D** (**MOSTLY** the first token) because:

1. The model decides on word endings in subsequent autoregressive passes.
2. When handling multi-token mappings, we have three options:
   - Use only the first token (`--weighting-decay-factor 0.0`)
   - Use a uniform mean of all tokens (`--weighting-decay-factor 1.0`)
   - Use exponentially decreasing weights (`--weighting-decay-factor 0.5`)
3. We choose to use `0.5` as the default because:
   - Using only the first token creates probability mass inflation for repeated prefixes.
   - Using a uniform mean inappropriately gives too much weight to trailing tokens.

### Mathematical Considerations

- Using means or scaling logits isn't mathematically ideal for probability distribution.
- Proper token splitting would require subtracting `log(n)` from each token in an n-token group.
- In the absence of an `lm_head.bias`, our approach provides the most practical solution.
- The `--weighting-decay-factor` parameter controls how we handle cases where one target token maps to multiple donor tokens. The default value of `0.5` balances between preserving the importance of the first token while still incorporating information from all tokens in the sequence. Values closer to `0.0` or `1.0` may provide better initialisations for fine-tuning but could produce less reliable outputs if used without any further fine-tuning.

## Credit

Original concept by [turboderp](https://huggingface.co/turboderp). Based on [original implementation](https://huggingface.co/turboderp/Qwama-0.5B-Instruct/blob/main/vocab_transplant.py).

## License

Apache 2.0 License - See [LICENSE](LICENSE) for details
