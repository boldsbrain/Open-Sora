# Define dataset
dataset = dict(
    type="VariableVideoTextDataset",
    data_path=None,
    num_frames=None,
    frame_interval=3,
    image_size=(None, None),
    transform_name="resize_crop",
)
bucket_config = {  # 6s/it
    "240p": {16: (1.0, 17), 32: (1.0, 9), 64: (1.0, 4), 128: (1.0, 2)},
    "256": {1: (1.0, 254)},
    "512": {1: (0.5, 86)},
    "480p": {1: (0.4, 54), 16: (0.4, 4), 32: (0.0, None)},
    "720p": {16: (0.1, 2), 32: (0.0, None)},  # No examples now
    "1024": {1: (0.3, 20)},
    "1080p": {1: (0.4, 8)},
}
mask_ratios = {
    "mask_no": 0.9,
    "mask_quarter_random": 0.01,
    "mask_quarter_head": 0.01,
    "mask_quarter_tail": 0.01,
    "mask_quarter_head_tail": 0.02,
    "mask_image_random": 0.01,
    "mask_image_head": 0.01,
    "mask_image_tail": 0.01,
    "mask_image_head_tail": 0.02,
}

# Define acceleration
num_workers = 4
dtype = "bf16"
grad_checkpoint = True
plugin = "zero2"
sp_size = 1

# Define model
model = dict(
    type="STDiT2-XL/2",
    from_pretrained=None,
    input_sq_size=512,  # pretrained model is trained on 512x512
    enable_flashattn=True,
    enable_layernorm_kernel=True,
)
vae = dict(
    type="VideoAutoencoderKL",
    from_pretrained="stabilityai/sd-vae-ft-ema",
    micro_batch_size=4,
)
text_encoder = dict(
    type="t5",
    from_pretrained="DeepFloyd/t5-v1_1-xxl",
    model_max_length=200,
    shardformer=True,
)
scheduler = dict(
    type="iddpm-speed",
    timestep_respacing="",
)

# Others
seed = 42
outputs = "outputs"
wandb = False

epochs = 1000
log_every = 10
ckpt_every = 500
load = None

batch_size = None
lr = 2e-5
grad_clip = 1.0
