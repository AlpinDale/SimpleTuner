from helpers.data_backend.local import LocalDataBackend
from helpers.data_backend.aws import S3DataBackend
from helpers.data_backend.base import BaseDataBackend
from helpers.caching.sdxl_embeds import TextEmbeddingCache

from helpers.training.exceptions import MultiDatasetExhausted
from helpers.multiaspect.dataset import MultiAspectDataset
from helpers.multiaspect.sampler import MultiAspectSampler
from helpers.prompts import PromptHandler
from helpers.caching.vae import VAECache
from helpers.training.multi_process import rank_info
from helpers.training.collate import collate_fn
from helpers.training.state_tracker import StateTracker

import json, os, torch, logging, io, time

logger = logging.getLogger("DataBackendFactory")


def init_backend_config(backend: dict, args: dict, accelerator) -> dict:
    output = {"id": backend["id"], "config": {}}
    if backend.get("dataset_type", None) == "text_embeds":
        if "caption_filter_list" in backend:
            output["config"]["caption_filter_list"] = backend["caption_filter_list"]
        output["dataset_type"] = "text_embeds"

        return output
    else:
        ## Check for settings we shouldn't have for non-text datasets.
        if "caption_filter_list" in backend:
            raise ValueError(
                f"caption_filter_list is only a valid setting for text datasets. It is currently set for the {backend.get('dataset_type', 'image')} dataset {backend['id']}."
            )

    # Image backend config
    output["dataset_type"] = backend.get("dataset_type", "image")
    choices = ["image", "conditioning"]
    if (
        StateTracker.get_args().controlnet
        and output["dataset_type"] == "image"
        and backend.get("conditioning_data", None) is None
    ):
        raise ValueError(
            "Image datasets require a corresponding conditioning_data set configured in your dataloader."
        )
    if output["dataset_type"] not in choices:
        raise ValueError(f"(id={backend['id']}) dataset_type must be one of {choices}.")
    if "vae_cache_clear_each_epoch" in backend:
        output["config"]["vae_cache_clear_each_epoch"] = backend[
            "vae_cache_clear_each_epoch"
        ]
    if "probability" in backend:
        output["config"]["probability"] = backend["probability"]
    if "ignore_epochs" in backend:
        output["config"]["ignore_epochs"] = backend["ignore_epochs"]
    if "repeats" in backend:
        output["config"]["repeats"] = backend["repeats"]
    if "crop" in backend:
        output["config"]["crop"] = backend["crop"]
    else:
        output["config"]["crop"] = args.crop
    if "crop_aspect" in backend:
        output["config"]["crop_aspect"] = backend["crop_aspect"]
    else:
        output["config"]["crop_aspect"] = "square"
    if "crop_style" in backend:
        crop_styles = ["random", "corner", "center", "centre", "face"]
        if backend["crop_style"] not in crop_styles:
            raise ValueError(
                f"(id={backend['id']}) crop_style must be one of {crop_styles}."
            )
        output["config"]["crop_style"] = backend["crop_style"]
    else:
        output["config"]["crop_style"] = "random"
    if "resolution" in backend:
        output["config"]["resolution"] = backend["resolution"]
    else:
        output["config"]["resolution"] = args.resolution
    if "resolution_type" in backend:
        output["config"]["resolution_type"] = backend["resolution_type"]
    else:
        output["config"]["resolution_type"] = args.resolution_type
    if "caption_strategy" in backend:
        output["config"]["caption_strategy"] = backend["caption_strategy"]
    else:
        output["config"]["caption_strategy"] = args.caption_strategy

    maximum_image_size = backend.get("maximum_image_size", args.maximum_image_size)
    target_downsample_size = backend.get(
        "target_downsample_size", args.target_downsample_size
    )
    output["config"]["maximum_image_size"] = maximum_image_size
    output["config"]["target_downsample_size"] = target_downsample_size

    if maximum_image_size and not target_downsample_size:
        raise ValueError(
            "When a data backend is configured to use `maximum_image_size`, you must also provide a value for `target_downsample_size`."
        )
    if (
        maximum_image_size
        and output["config"]["resolution_type"] == "area"
        and maximum_image_size > 10
        and not os.environ.get("SIMPLETUNER_MAXIMUM_IMAGE_SIZE_OVERRIDE", False)
    ):
        raise ValueError(
            f"When a data backend is configured to use `'resolution_type':area`, `maximum_image_size` must be less than 10 megapixels. You may have accidentally entered {maximum_image_size} pixels, instead of megapixels."
        )
    elif (
        maximum_image_size
        and output["config"]["resolution_type"] == "pixel"
        and maximum_image_size < 512
        and "deepfloyd" not in args.model_type
    ):
        raise ValueError(
            f"When a data backend is configured to use `'resolution_type':pixel`, `maximum_image_size` must be at least 512 pixels. You may have accidentally entered {maximum_image_size} megapixels, instead of pixels."
        )
    if (
        target_downsample_size
        and output["config"]["resolution_type"] == "area"
        and target_downsample_size > 10
        and not os.environ.get("SIMPLETUNER_MAXIMUM_IMAGE_SIZE_OVERRIDE", False)
    ):
        raise ValueError(
            f"When a data backend is configured to use `'resolution_type':area`, `target_downsample_size` must be less than 10 megapixels. You may have accidentally entered {target_downsample_size} pixels, instead of megapixels."
        )
    elif (
        target_downsample_size
        and output["config"]["resolution_type"] == "pixel"
        and target_downsample_size < 512
        and "deepfloyd" not in args.model_type
    ):
        raise ValueError(
            f"When a data backend is configured to use `'resolution_type':pixel`, `target_downsample_size` must be at least 512 pixels. You may have accidentally entered {target_downsample_size} megapixels, instead of pixels."
        )

    return output


def print_bucket_info(metadata_backend):
    # Print table header
    print(f"{rank_info()} | {'Bucket':<10} | {'Image Count':<12}")

    # Print separator
    print("-" * 30)

    # Print each bucket's information
    for bucket in metadata_backend.aspect_ratio_bucket_indices:
        image_count = len(metadata_backend.aspect_ratio_bucket_indices[bucket])
        if image_count == 0:
            continue
        print(f"{rank_info()} | {bucket:<10} | {image_count:<12}")


def configure_parquet_database(backend: dict, args, data_backend: BaseDataBackend):
    """When given a backend config dictionary, configure a parquet database."""
    parquet_config = backend.get("parquet", None)
    if not parquet_config:
        raise ValueError(
            f"Parquet backend must have a 'parquet' field in the backend config containing required fields for configuration."
        )
    parquet_path = parquet_config.get("path", None)
    if not parquet_path:
        raise ValueError(
            "Parquet backend must have a 'path' field in the backend config under the 'parquet' key."
        )
    if not data_backend.exists(parquet_path):
        raise FileNotFoundError(f"Parquet file {parquet_path} not found.")
    # Load the dataframe
    import pandas as pd

    bytes_string = data_backend.read(parquet_path)
    pq = io.BytesIO(bytes_string)
    df = pd.read_parquet(pq)
    caption_column = parquet_config.get(
        "caption_column", args.parquet_caption_column or "description"
    )
    fallback_caption_column = parquet_config.get("fallback_caption_column", None)
    filename_column = parquet_config.get(
        "filename_column", args.parquet_filename_column or "id"
    )
    identifier_includes_extension = parquet_config.get(
        "identifier_includes_extension", False
    )

    # Check the columns exist
    if caption_column not in df.columns:
        raise ValueError(
            f"Parquet file {parquet_path} does not contain a column named '{caption_column}'."
        )
    if filename_column not in df.columns:
        raise ValueError(
            f"Parquet file {parquet_path} does not contain a column named '{filename_column}'."
        )
    # Check for null values
    if df[caption_column].isnull().values.any() and not fallback_caption_column:
        raise ValueError(
            f"Parquet file {parquet_path} contains null values in the '{caption_column}' column, but no fallback_caption_column was set."
        )
    if df[filename_column].isnull().values.any():
        raise ValueError(
            f"Parquet file {parquet_path} contains null values in the '{filename_column}' column."
        )
    # Check for empty strings
    if (df[caption_column] == "").sum() > 0 and not fallback_caption_column:
        raise ValueError(
            f"Parquet file {parquet_path} contains empty strings in the '{caption_column}' column."
        )
    if (df[filename_column] == "").sum() > 0:
        raise ValueError(
            f"Parquet file {parquet_path} contains empty strings in the '{filename_column}' column."
        )
    # Store the database in StateTracker
    StateTracker.set_parquet_database(
        backend["id"],
        (
            df,
            filename_column,
            caption_column,
            fallback_caption_column,
            identifier_includes_extension,
        ),
    )
    logger.info(
        f"Configured parquet database for backend {backend['id']}. Caption column: {caption_column}. Filename column: {filename_column}."
    )


def configure_multi_databackend(
    args: dict, accelerator, text_encoders, tokenizers, prompt_handler
):
    """
    Configure a multiple dataloaders based on the provided commandline args.
    """
    logger.setLevel(
        os.environ.get(
            "SIMPLETUNER_LOG_LEVEL", "INFO" if accelerator.is_main_process else "ERROR"
        )
    )
    if args.data_backend_config is None:
        raise ValueError(
            "Must provide a data backend config file via --data_backend_config"
        )
    if not os.path.exists(args.data_backend_config):
        raise FileNotFoundError(
            f"Data backend config file {args.data_backend_config} not found."
        )
    logger.info(f"Loading data backend config from {args.data_backend_config}")
    with open(args.data_backend_config, "r") as f:
        data_backend_config = json.load(f)
    if len(data_backend_config) == 0:
        raise ValueError(
            "Must provide at least one data backend in the data backend config file."
        )

    text_embed_backends = {}
    default_text_embed_backend_id = None
    for backend in data_backend_config:
        dataset_type = backend.get("dataset_type", None)
        if dataset_type is None or dataset_type != "text_embeds":
            # Skip configuration of image data backends. It is done later.
            continue
        if ("disabled" in backend and backend["disabled"]) or (
            "disable" in backend and backend["disable"]
        ):
            logger.info(
                f"Skipping disabled data backend {backend['id']} in config file."
            )
            continue

        logger.info(f'Configuring text embed backend: {backend["id"]}')
        if backend.get("default", None):
            if default_text_embed_backend_id is not None:
                raise ValueError(
                    "Only one text embed backend can be marked as default."
                )
            default_text_embed_backend_id = backend["id"]
        # Retrieve some config file overrides for commandline arguments,
        #  there currently isn't much for text embeds.
        init_backend = init_backend_config(backend, args, accelerator)
        StateTracker.set_data_backend_config(init_backend["id"], init_backend["config"])
        if backend["type"] == "local":
            init_backend["data_backend"] = get_local_backend(
                accelerator, init_backend["id"]
            )
            init_backend["cache_dir"] = backend["cache_dir"]
        elif backend["type"] == "aws":
            check_aws_config(backend)
            init_backend["data_backend"] = get_aws_backend(
                identifier=init_backend["id"],
                aws_bucket_name=backend["aws_bucket_name"],
                aws_region_name=backend["aws_region_name"],
                aws_endpoint_url=backend["aws_endpoint_url"],
                aws_access_key_id=backend["aws_access_key_id"],
                aws_secret_access_key=backend["aws_secret_access_key"],
                accelerator=accelerator,
            )
            # S3 buckets use the aws_data_prefix as their prefix/ for all data.
            # Ensure we have a trailing slash on the prefix:
            init_backend["cache_dir"] = backend.get("aws_data_prefix", None)
        else:
            raise ValueError(f"Unknown data backend type: {backend['type']}")

        preserve_data_backend_cache = backend.get("preserve_data_backend_cache", False)
        if not preserve_data_backend_cache and accelerator.is_local_main_process:
            StateTracker.delete_cache_files(
                data_backend_id=init_backend["id"],
                preserve_data_backend_cache=preserve_data_backend_cache,
            )

        # Generate a TextEmbeddingCache object
        init_backend["text_embed_cache"] = TextEmbeddingCache(
            id=init_backend["id"],
            data_backend=init_backend["data_backend"],
            text_encoders=text_encoders,
            tokenizers=tokenizers,
            accelerator=accelerator,
            cache_dir=init_backend.get("cache_dir", args.cache_dir_text),
            model_type=StateTracker.get_model_type(),
            write_batch_size=backend.get("write_batch_size", 1),
        )

        if backend.get("default", False):
            # The default embed cache will be used for eg. validation prompts.
            StateTracker.set_default_text_embed_cache(init_backend["text_embed_cache"])
            logger.debug(f"Set the default text embed cache to {init_backend['id']}.")
            # We will compute the null embedding for caption dropout here.
            logger.info("Pre-computing null embedding")
            with accelerator.main_process_first():
                init_backend["text_embed_cache"].compute_embeddings_for_prompts(
                    [""], return_concat=False, load_from_cache=False
                )
            time.sleep(5)
            accelerator.wait_for_everyone()
        if args.caption_dropout_probability == 0.0:
            logger.warning(
                f"Not using caption dropout will potentially lead to overfitting on captions, eg. CFG will not work very well. Set --caption-dropout_probability=0.1 as a recommended value."
            )

        # We don't compute the text embeds at this time, because we do not really have any captions available yet.
        text_embed_backends[init_backend["id"]] = init_backend

    if not text_embed_backends:
        raise ValueError(
            "Must provide at least one text embed backend in the data backend config file."
        )
    if not default_text_embed_backend_id and len(text_embed_backends) > 1:
        raise ValueError(
            "Must provide a default text embed backend in the data backend config file. It requires 'default':true."
        )
    elif not default_text_embed_backend_id:
        logger.warning(
            f"No default text embed was defined, using {list(text_embed_backends.keys())[0]} as the default."
        )
        default_text_embed_backend_id = list(text_embed_backends.keys())[0]
    logger.info("Completed loading text embed services.")

    all_captions = []
    for backend in data_backend_config:
        dataset_type = backend.get("dataset_type", None)
        if dataset_type is not None and (
            dataset_type != "image" and dataset_type != "conditioning"
        ):
            # Skip configuration of text embed backends. It is done earlier.
            continue
        if ("disabled" in backend and backend["disabled"]) or (
            "disable" in backend and backend["disable"]
        ):
            logger.info(
                f"Skipping disabled data backend {backend['id']} in config file."
            )
            continue
        # For each backend, we will create a dict to store all of its components in.
        if (
            "id" not in backend
            or backend["id"] == ""
            or backend["id"] in StateTracker.get_data_backends()
        ):
            raise ValueError("Each dataset needs a unique 'id' field.")
        logger.info(f"Configuring data backend: {backend['id']}")
        # Retrieve some config file overrides for commandline arguments, eg. cropping
        init_backend = init_backend_config(backend, args, accelerator)
        logger.info(f"Configured backend: {init_backend}")
        StateTracker.set_data_backend_config(
            data_backend_id=init_backend["id"],
            config=init_backend["config"],
        )

        preserve_data_backend_cache = backend.get("preserve_data_backend_cache", False)
        if not preserve_data_backend_cache:
            StateTracker.delete_cache_files(
                data_backend_id=init_backend["id"],
                preserve_data_backend_cache=preserve_data_backend_cache,
            )

        if backend["type"] == "local":
            init_backend["data_backend"] = get_local_backend(
                accelerator, init_backend["id"]
            )
            init_backend["instance_data_root"] = backend["instance_data_dir"]
            # Remove trailing slash
            if init_backend["instance_data_root"][-1] == "/":
                init_backend["instance_data_root"] = init_backend["instance_data_root"][
                    :-1
                ]
        elif backend["type"] == "aws":
            check_aws_config(backend)
            init_backend["data_backend"] = get_aws_backend(
                identifier=init_backend["id"],
                aws_bucket_name=backend["aws_bucket_name"],
                aws_region_name=backend["aws_region_name"],
                aws_endpoint_url=backend["aws_endpoint_url"],
                aws_access_key_id=backend["aws_access_key_id"],
                aws_secret_access_key=backend["aws_secret_access_key"],
                accelerator=accelerator,
            )
            # S3 buckets use the aws_data_prefix as their prefix/ for all data.
            init_backend["instance_data_root"] = backend["aws_data_prefix"]
        else:
            raise ValueError(f"Unknown data backend type: {backend['type']}")

        # Assign a TextEmbeddingCache to this dataset. it might be undefined.
        text_embed_id = backend.get("text_embeds", default_text_embed_backend_id)
        if text_embed_id not in text_embed_backends:
            raise ValueError(
                f"Text embed backend {text_embed_id} not found in data backend config file."
            )
        logger.info(f"(id={init_backend['id']}) Loading bucket manager.")
        metadata_backend_args = {}
        metadata_backend = backend.get("metadata_backend", "json")
        if metadata_backend == "json":
            from helpers.metadata.backends.json import JsonMetadataBackend

            BucketManager_cls = JsonMetadataBackend
        elif metadata_backend == "parquet":
            from helpers.metadata.backends.parquet import ParquetMetadataBackend

            BucketManager_cls = ParquetMetadataBackend
            metadata_backend_args["parquet_config"] = backend.get("parquet", None)
            if not metadata_backend_args["parquet_config"]:
                raise ValueError(
                    f"Parquet metadata backend requires a 'parquet' field in the backend config containing required fields for configuration."
                )
        else:
            raise ValueError(f"Unknown metadata backend type: {metadata_backend}")
        init_backend["metadata_backend"] = BucketManager_cls(
            id=init_backend["id"],
            instance_data_root=init_backend["instance_data_root"],
            data_backend=init_backend["data_backend"],
            accelerator=accelerator,
            resolution=backend.get("resolution", args.resolution),
            minimum_image_size=backend.get(
                "minimum_image_size", args.minimum_image_size
            ),
            resolution_type=backend.get("resolution_type", args.resolution_type),
            batch_size=args.train_batch_size,
            metadata_update_interval=backend.get(
                "metadata_update_interval", args.metadata_update_interval
            ),
            cache_file=os.path.join(
                init_backend["instance_data_root"],
                "aspect_ratio_bucket_indices",
            ),
            metadata_file=os.path.join(
                init_backend["instance_data_root"],
                "aspect_ratio_bucket_metadata",
            ),
            delete_problematic_images=args.delete_problematic_images or False,
            cache_file_suffix=backend.get("cache_file_suffix", None),
            **metadata_backend_args,
        )

        if "aspect" not in args.skip_file_discovery or "aspect" not in backend.get(
            "skip_file_discovery", ""
        ):
            if accelerator.is_local_main_process:
                logger.info(
                    f"(id={init_backend['id']}) Refreshing aspect buckets on main process."
                )
                init_backend["metadata_backend"].refresh_buckets(rank_info())
        accelerator.wait_for_everyone()
        if not accelerator.is_main_process:
            logger.info(
                f"(id={init_backend['id']}) Reloading bucket manager cache on subprocesses."
            )
            init_backend["metadata_backend"].reload_cache()
        accelerator.wait_for_everyone()
        if init_backend["metadata_backend"].has_single_underfilled_bucket():
            raise Exception(
                f"Cannot train using a dataset that has a single bucket with fewer than {args.train_batch_size} images."
                f" You have to reduce your batch size, or increase your dataset size (id={init_backend['id']})."
            )
        # Now split the contents of these buckets between all processes
        init_backend["metadata_backend"].split_buckets_between_processes(
            gradient_accumulation_steps=args.gradient_accumulation_steps,
        )

        # Check if there is an existing 'config' in the metadata_backend.config
        excluded_keys = [
            "probability",
            "repeats",
            "ignore_epochs",
            "caption_filter_list",
            "vae_cache_clear_each_epoch",
            "caption_strategy",
            "maximum_image_size",
            "target_downsample_size",
            "parquet",
        ]
        if init_backend["metadata_backend"].config != {}:
            prev_config = init_backend["metadata_backend"].config
            logger.debug(f"Found existing config: {prev_config}")
            logger.debug(f"Comparing against new config: {init_backend['config']}")
            # Check if any values differ between the 'backend' values and the 'config' values:
            for key, _ in prev_config.items():
                logger.debug(f"Checking config key: {key}")
                if (
                    key in backend
                    and prev_config[key] != backend[key]
                    and key not in excluded_keys
                ):
                    if not args.override_dataset_config:
                        raise Exception(
                            f"Dataset {init_backend['id']} has inconsistent config, and --override_dataset_config was not provided."
                            f"\n-> Expected value {key}={prev_config[key]} differs from current value={backend[key]}."
                            f"\n-> Recommended action is to correct the current config values to match the values that were used to create this dataset:"
                            f"\n{prev_config}"
                        )
                    else:
                        logger.warning(
                            f"Overriding config value {key}={prev_config[key]} with {backend[key]}"
                        )
                        prev_config[key] = backend[key]
        StateTracker.set_data_backend_config(init_backend["id"], init_backend["config"])
        logger.info(f"Configured backend: {init_backend}")

        print_bucket_info(init_backend["metadata_backend"])
        if len(init_backend["metadata_backend"]) == 0:
            raise Exception(
                f"No images were discovered by the bucket manager in the dataset: {init_backend['id']}."
            )

        use_captions = True
        if "only_instance_prompt" in backend and backend["only_instance_prompt"]:
            use_captions = False
        elif args.only_instance_prompt:
            use_captions = False
        init_backend["train_dataset"] = MultiAspectDataset(
            id=init_backend["id"],
            datasets=[init_backend["metadata_backend"]],
        )

        if "deepfloyd" in args.model_type:
            if init_backend["metadata_backend"].resolution_type == "area":
                logger.warning(
                    "Resolution type is 'area', but should be 'pixel' for DeepFloyd. Unexpected results may occur."
                )
                if init_backend["metadata_backend"].resolution > 0.25:
                    logger.warning(
                        "Resolution is greater than 0.25 megapixels. This may lead to unconstrained memory requirements."
                    )
            if init_backend["metadata_backend"].resolution_type == "pixel":
                if (
                    "stage2" not in args.model_type
                    and init_backend["metadata_backend"].resolution > 64
                ):
                    logger.warning(
                        "Resolution is greater than 64 pixels, which will possibly lead to poor quality results."
                    )

        if "deepfloyd-stage2" in args.model_type:
            # Resolution must be at least 256 for Stage II.
            if init_backend["metadata_backend"].resolution < 256:
                logger.warning(
                    "Increasing resolution to 256, as is required for DF Stage II."
                )

        init_backend["sampler"] = MultiAspectSampler(
            id=init_backend["id"],
            metadata_backend=init_backend["metadata_backend"],
            data_backend=init_backend["data_backend"],
            accelerator=accelerator,
            batch_size=args.train_batch_size,
            debug_aspect_buckets=args.debug_aspect_buckets,
            delete_unwanted_images=backend.get(
                "delete_unwanted_images", args.delete_unwanted_images
            ),
            resolution=backend.get("resolution", args.resolution),
            resolution_type=backend.get("resolution_type", args.resolution_type),
            caption_strategy=backend.get("caption_strategy", args.caption_strategy),
            use_captions=use_captions,
            prepend_instance_prompt=backend.get(
                "prepend_instance_prompt", args.prepend_instance_prompt
            ),
            instance_prompt=backend.get("instance_prompt", args.instance_prompt),
        )
        if init_backend["sampler"].caption_strategy == "parquet":
            configure_parquet_database(backend, args, init_backend["data_backend"])
        init_backend["train_dataloader"] = torch.utils.data.DataLoader(
            init_backend["train_dataset"],
            batch_size=1,  # The sampler handles batching
            shuffle=False,  # The sampler handles shuffling
            sampler=init_backend["sampler"],
            collate_fn=lambda examples: collate_fn(examples),
            num_workers=0,
            persistent_workers=False,
        )

        init_backend["text_embed_cache"] = text_embed_backends[text_embed_id][
            "text_embed_cache"
        ]
        prepend_instance_prompt = backend.get(
            "prepend_instance_prompt", args.prepend_instance_prompt
        )
        instance_prompt = backend.get("instance_prompt", args.instance_prompt)
        if prepend_instance_prompt and instance_prompt is None:
            raise ValueError(
                f"Backend {init_backend['id']} has prepend_instance_prompt=True, but no instance_prompt was provided. You must provide an instance_prompt, or disable this option."
            )

        # We get captions from the IMAGE dataset. Not the text embeds dataset.
        if "text" not in args.skip_file_discovery and "text" not in backend.get(
            "skip_file_discovery", ""
        ):
            logger.info(f"(id={init_backend['id']}) Collecting captions.")
            captions = PromptHandler.get_all_captions(
                data_backend=init_backend["data_backend"],
                instance_data_root=init_backend["instance_data_root"],
                prepend_instance_prompt=prepend_instance_prompt,
                instance_prompt=instance_prompt,
                use_captions=use_captions,
                caption_strategy=backend.get("caption_strategy", args.caption_strategy),
            )
            logger.debug(
                f"Pre-computing text embeds / updating cache. We have {len(captions)} captions to process, though these will be filtered next."
            )
            caption_strategy = backend.get("caption_strategy", args.caption_strategy)
            logger.info(
                f"(id={init_backend['id']}) Initialise text embed pre-computation using the {caption_strategy} caption strategy. We have {len(captions)} captions to process."
            )
            init_backend["text_embed_cache"].compute_embeddings_for_prompts(
                captions, return_concat=False, load_from_cache=False
            )
            logger.info(
                f"(id={init_backend['id']}) Completed processing {len(captions)} captions."
            )

        if "deepfloyd" not in StateTracker.get_args().model_type:
            logger.info(f"(id={init_backend['id']}) Creating VAE latent cache.")
            init_backend["vaecache"] = VAECache(
                id=init_backend["id"],
                vae=StateTracker.get_vae(),
                accelerator=accelerator,
                metadata_backend=init_backend["metadata_backend"],
                data_backend=init_backend["data_backend"],
                instance_data_root=init_backend["instance_data_root"],
                delete_problematic_images=backend.get(
                    "delete_problematic_images", args.delete_problematic_images
                ),
                resolution=backend.get("resolution", args.resolution),
                resolution_type=backend.get("resolution_type", args.resolution_type),
                maximum_image_size=backend.get(
                    "maximum_image_size",
                    args.maximum_image_size
                    or backend.get("resolution", args.resolution) * 1.5,
                ),
                target_downsample_size=backend.get(
                    "target_downsample_size",
                    args.target_downsample_size
                    or backend.get("resolution", args.resolution) * 1.5,
                ),
                minimum_image_size=backend.get(
                    "minimum_image_size",
                    args.minimum_image_size
                    or backend.get("resolution", args.resolution),
                ),
                vae_batch_size=args.vae_batch_size,
                write_batch_size=args.write_batch_size,
                cache_dir=backend.get("cache_dir_vae", args.cache_dir_vae),
                max_workers=backend.get("max_workers", 32),
                vae_cache_preprocess=args.vae_cache_preprocess,
            )

            if args.vae_cache_preprocess:
                logger.info(f"(id={init_backend['id']}) Discovering cache objects..")
                if accelerator.is_local_main_process:
                    init_backend["vaecache"].discover_all_files()
                accelerator.wait_for_everyone()

        if (
            (
                "metadata" not in args.skip_file_discovery
                or "metadata" not in backend.get("skip_file_discovery", "")
            )
            and accelerator.is_main_process
            and backend.get("scan_for_errors", False)
            and "deepfloyd" not in StateTracker.get_args().model_type
        ):
            logger.info(
                f"Beginning error scan for dataset {init_backend['id']}. Set 'scan_for_errors' to False in the dataset config to disable this."
            )
            init_backend["metadata_backend"].handle_vae_cache_inconsistencies(
                vae_cache=init_backend["vaecache"],
                vae_cache_behavior=backend.get(
                    "vae_cache_scan_behaviour", args.vae_cache_scan_behaviour
                ),
            )
            init_backend["metadata_backend"].scan_for_metadata()
        elif not backend.get("scan_for_errors", False):
            logger.info(
                f"Skipping error scan for dataset {init_backend['id']}. Set 'scan_for_errors' to True in the dataset config to enable this if your training runs into mismatched latent dimensions."
            )
        accelerator.wait_for_everyone()
        if not accelerator.is_main_process:
            init_backend["metadata_backend"].load_image_metadata()
        accelerator.wait_for_everyone()

        if (
            args.vae_cache_preprocess
            and "vae" not in args.skip_file_discovery
            and "vae" not in backend.get("skip_file_discovery", "")
        ):
            init_backend["vaecache"].split_cache_between_processes()
            if args.vae_cache_preprocess:
                init_backend["vaecache"].process_buckets()
            logger.info(
                f"Encoding images during training: {not args.vae_cache_preprocess}"
            )
            accelerator.wait_for_everyone()

        StateTracker.register_data_backend(init_backend)
        init_backend["metadata_backend"].save_cache()

    # After configuring all backends, register their captions.
    StateTracker.set_caption_files(all_captions)

    # For each image backend, connect it to its conditioning backend.
    for backend in data_backend_config:
        dataset_type = backend.get("dataset_type", "image")
        if dataset_type is not None and dataset_type != "image":
            # Skip configuration of conditioning/text data backends. It is done earlier.
            continue
        if ("disabled" in backend and backend["disabled"]) or (
            "disable" in backend and backend["disable"]
        ):
            logger.info(
                f"Skipping disabled data backend {backend['id']} in config file."
            )
            continue
        if "conditioning_data" in backend and backend[
            "conditioning_data"
        ] not in StateTracker.get_data_backends(_type="conditioning"):
            raise ValueError(
                f"Conditioning data backend {backend['conditioning_data']} not found in data backend list: {StateTracker.get_data_backends()}."
            )
        if "conditioning_data" in backend:
            StateTracker.set_conditioning_dataset(
                backend["id"], backend["conditioning_data"]
            )
            logger.info(
                f"Successfully configured conditioning image dataset for {backend['id']}"
            )

    if len(StateTracker.get_data_backends()) == 0:
        raise ValueError(
            "Must provide at least one data backend in the data backend config file."
        )
    return StateTracker.get_data_backends()


def get_local_backend(accelerator, identifier: str) -> LocalDataBackend:
    """
    Get a local disk backend.

    Args:
        accelerator (Accelerator): A Huggingface Accelerate object.
        identifier (str): An identifier that links this data backend to its other components.
    Returns:
        LocalDataBackend: A LocalDataBackend object.
    """
    return LocalDataBackend(accelerator=accelerator, id=identifier)


def check_aws_config(backend: dict) -> None:
    """
    Check the configuration for an AWS backend.

    Args:
        backend (dict): A dictionary of the backend configuration.
    Returns:
        None
    """
    required_keys = [
        "aws_bucket_name",
        "aws_region_name",
        "aws_endpoint_url",
        "aws_access_key_id",
        "aws_secret_access_key",
    ]
    for key in required_keys:
        if key not in backend:
            raise ValueError(f"Missing required key {key} in AWS backend config.")


def get_aws_backend(
    aws_bucket_name: str,
    aws_region_name: str,
    aws_endpoint_url: str,
    aws_access_key_id: str,
    aws_secret_access_key: str,
    accelerator,
    identifier: str,
) -> S3DataBackend:
    return S3DataBackend(
        id=identifier,
        bucket_name=aws_bucket_name,
        accelerator=accelerator,
        region_name=aws_region_name,
        endpoint_url=aws_endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )


step = None


def select_dataloader_index(step, backends):
    # Generate weights for each backend based on some criteria
    weights = []
    backend_ids = []
    for backend_id, backend in backends.items():
        weight = get_backend_weight(backend_id, backend, step)
        weights.append(weight)
        backend_ids.append(backend_id)

    # Convert to a torch tensor for easy sampling
    weights = torch.tensor(weights)
    weights /= weights.sum()  # Normalize the weights

    if weights.sum() == 0:
        return None

    # Sample a backend index based on the weights
    chosen_index = torch.multinomial(weights, 1).item()
    return backend_ids[chosen_index]


def get_backend_weight(backend_id, backend, step):
    # Implement your logic to determine the weight for each backend
    # For example, a simple linear decay based on the step count
    backend_config = StateTracker.get_data_backend_config(backend_id)
    prob = backend_config.get("probability", 1)
    disable_step = backend_config.get("disable_after_epoch_step", float("inf"))
    adjusted_prob = (
        0 if step > disable_step else max(0, prob * (1 - step / disable_step))
    )
    return adjusted_prob


def random_dataloader_iterator(backends: dict):
    global step
    if step is None:
        step = StateTracker.get_epoch_step()
    else:
        step = 0

    gradient_accumulation_steps = StateTracker.get_args().gradient_accumulation_steps
    logger.debug(f"Backends to select from {backends}")
    while backends:
        step += 1
        epoch_step = int(step / gradient_accumulation_steps)
        StateTracker.set_epoch_step(epoch_step)

        chosen_backend_id = select_dataloader_index(step, backends)
        if chosen_backend_id is None:
            logger.info("No dataloader iterators were available.")
            break

        chosen_iter = iter(backends[chosen_backend_id])

        try:
            yield (step, next(chosen_iter))
        except MultiDatasetExhausted:
            # We may want to repeat the same dataset multiple times in a single epoch.
            # If so, we can just reset the iterator and keep going.
            repeats = StateTracker.get_data_backend_config(chosen_backend_id).get(
                "repeats", False
            )
            if (
                repeats
                and repeats > 0
                and StateTracker.get_repeats(chosen_backend_id) < repeats
            ):
                StateTracker.increment_repeats(chosen_backend_id)
                logger.debug(
                    f"Dataset (name={chosen_backend_id}) is now sampling its {StateTracker.get_repeats(chosen_backend_id)} repeat out of {repeats} total allowed."
                )
                continue
            logger.debug(
                f"Dataset (name={chosen_backend_id}) is now exhausted after {StateTracker.get_repeats(chosen_backend_id)} repeat(s). Removing from list."
            )
            del backends[chosen_backend_id]
            StateTracker.backend_exhausted(chosen_backend_id)
            StateTracker.set_repeats(data_backend_id=chosen_backend_id, repeats=0)
        finally:
            if not backends or all(
                [
                    StateTracker.get_data_backend_config(backend_id).get(
                        "ignore_epochs", False
                    )
                    for backend_id in backends
                ]
            ):
                logger.debug(
                    "All dataloaders exhausted. Moving to next epoch in main training loop."
                )
                StateTracker.clear_exhausted_buckets()
                return (step, None)
