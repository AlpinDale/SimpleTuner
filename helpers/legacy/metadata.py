import os
from helpers.training.state_tracker import StateTracker


def lora_info(args):
    """Return a string with the LORA information."""
    if "lora" not in args.model_type:
        return ""
    return f"""- LoRA Rank: {args.lora_rank}
- LoRA Alpha: {args.lora_alpha}
- LoRA Dropout: {args.lora_dropout}
- LoRA initialisation style: {args.lora_init_type}
"""


def save_model_card(
    repo_id: str,
    images=None,
    base_model: str = "",
    train_text_encoder: bool = False,
    prompt: str = "",
    validation_prompts: dict = None,
    repo_folder: str = None,
):
    if repo_folder is None:
        raise ValueError("The repo_folder must be specified and not be None.")

    assets_folder = os.path.join(repo_folder, "assets")
    os.makedirs(assets_folder, exist_ok=True)
    datasets_str = ""
    for dataset in StateTracker.get_data_backends().keys():
        if "sampler" in StateTracker.get_data_backends()[dataset]:
            datasets_str += f"### {dataset}\n"
            datasets_str += f"{StateTracker.get_data_backends()[dataset]['sampler'].log_state(show_rank=False, alt_stats=True)}"
    widget_str = ""
    idx = 0
    shortname_idx = 0
    if images:
        widget_str = "widget:"
        for image_list in images.values() if isinstance(images, dict) else images:
            if not isinstance(image_list, list):
                image_list = [image_list]
            sub_idx = 0
            for image in image_list:
                image_path = os.path.join(assets_folder, f"image_{idx}_{sub_idx}.png")
                image.save(image_path, format="PNG")
                validation_prompt = (
                    validation_prompts[shortname_idx]
                    if validation_prompts and shortname_idx in validation_prompts
                    else "no prompt available"
                )
                if validation_prompt == "":
                    validation_prompt = "unconditional (blank prompt)"
                else:
                    # Escape anything that YAML won't like
                    validation_prompt = validation_prompt.replace("'", "''")
                widget_str += f"\n- text: '{validation_prompt}'"
                widget_str += f"\n  parameters:"
                widget_str += f"\n    negative_prompt: '{str(StateTracker.get_args().validation_negative_prompt)}'"
                widget_str += f"\n  output:"
                widget_str += f"\n    url: ./assets/image_{idx}_{sub_idx}.png"
                idx += 1
                sub_idx += 1

            shortname_idx += 1
    yaml_content = f"""---
license: creativeml-openrail-m
base_model: "{base_model}"
tags:
  - {'stable-diffusion' if 'deepfloyd' not in StateTracker.get_args().model_type else 'deepfloyd-if'}
  - {'stable-diffusion-diffusers' if 'deepfloyd' not in StateTracker.get_args().model_type else 'deepfloyd-if-diffusers'}
  - text-to-image
  - diffusers
  - {StateTracker.get_args().model_type}
{'  - template:sd-lora' if 'lora' in StateTracker.get_args().model_type else ''}
inference: true
{widget_str}
---

"""
    model_card_content = f"""# {repo_id}

This is a {'LoRA' if 'lora' in StateTracker.get_args().model_type else 'full rank'} finetuned model derived from [{base_model}](https://huggingface.co/{base_model}).

{'The main validation prompt used during training was:' if prompt else 'No validation prompt was used during training.'}

```
{prompt}
```

## Validation settings
- CFG: `{StateTracker.get_args().validation_guidance}`
- CFG Rescale: `{StateTracker.get_args().validation_guidance_rescale}`
- Steps: `{StateTracker.get_args().validation_num_inference_steps}`
- Sampler: `{StateTracker.get_args().validation_noise_scheduler}`
- Seed: `{StateTracker.get_args().validation_seed}`
- Resolution{'s' if ',' in StateTracker.get_args().validation_resolution else ''}: `{StateTracker.get_args().validation_resolution}`

Note: The validation settings are not necessarily the same as the [training settings](#training-settings).

{'You can find some example images in the following gallery:' if images is not None else ''}\n

<Gallery />

The text encoder {'**was**' if train_text_encoder else '**was not**'} trained.
{'You may reuse the base model text encoder for inference.' if not train_text_encoder else 'If the text encoder from this repository is not used at inference time, unexpected or bad results could occur.'}


## Training settings

- Training epochs: {StateTracker.get_epoch() - 1}
- Training steps: {StateTracker.get_global_step()}
- Learning rate: {StateTracker.get_args().learning_rate}
- Effective batch size: {StateTracker.get_args().train_batch_size * StateTracker.get_args().gradient_accumulation_steps}
  - Micro-batch size: {StateTracker.get_args().train_batch_size}
  - Gradient accumulation steps: {StateTracker.get_args().gradient_accumulation_steps}
- Prediction type: {StateTracker.get_args().prediction_type}
- Rescaled betas zero SNR: {StateTracker.get_args().rescale_betas_zero_snr}
- Optimizer: {'AdamW, stochastic bf16' if StateTracker.get_args().adam_bfloat16 else 'AdamW8Bit' if StateTracker.get_args().use_8bit_adam else 'Adafactor' if StateTracker.get_args().use_adafactor_optimizer else 'Prodigy' if StateTracker.get_args().use_prodigy_optimizer else 'AdamW'}
- Precision: {'Pure BF16' if StateTracker.get_args().adam_bfloat16 else StateTracker.get_args().mixed_precision}
- Xformers: {'Enabled' if StateTracker.get_args().enable_xformers_memory_efficient_attention else 'Not used'}
{lora_info(args=StateTracker.get_args())}

## Datasets

{datasets_str}
"""

    print(f"YAML:\n{yaml_content}")
    print(f"Model Card:\n{model_card_content}")
    with open(os.path.join(repo_folder, "README.md"), "w") as f:
        f.write(yaml_content + model_card_content)
