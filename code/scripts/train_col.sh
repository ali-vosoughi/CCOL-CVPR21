#!/bin/bash

OPTS=""
OPTS+="--id MUSIC_COL "
# OPTS+="--list_train /localdisk2/dat/sdeng10/MUSIC/train.csv "
# OPTS+="--list_val /localdisk2/dat/sdeng10/MUSIC/val.csv "
OPTS+="--list_train ../data/Music/train.csv "
OPTS+="--list_val ../data/Music/val.csv "

# Models
OPTS+="--arch_sound_ground vggish "
OPTS+="--arch_frame_ground resnet18 "
OPTS+="--arch_sound unet7 "
OPTS+="--arch_synthesizer linear "
OPTS+="--arch_grounding base "
OPTS+="--arch_frame resnet18dilated "
OPTS+="--img_pool maxpool "
OPTS+="--num_channels 32 "

## weights
OPTS+="--weights_sound_ground  ../data/ckpt/MUSIC_GRD-4mix-LogFreq-resnet18dilated-unet7-linear-frames3stride1-maxpool-ratio-weightedLoss-channels32-epoch40-step20_30/sound_ground_latest.pth "
OPTS+="--weights_frame_ground  ../data/ckpt/MUSIC_GRD-4mix-LogFreq-resnet18dilated-unet7-linear-frames3stride1-maxpool-ratio-weightedLoss-channels32-epoch40-step20_30/frame_ground_latest.pth "
OPTS+="--weights_grounding ../data/ckpt/MUSIC_GRD-4mix-LogFreq-resnet18dilated-unet7-linear-frames3stride1-maxpool-ratio-weightedLoss-channels32-epoch40-step20_30/grounding_latest.pth "

# binary mask, BCE loss, weighted loss
OPTS+="--binary_mask 0 "
OPTS+="--loss l1 "
OPTS+="--weighted_loss 1 "
# logscale in frequency
OPTS+="--num_mix 4 "
OPTS+="--log_freq 1 "

# frames-related
OPTS+="--num_frames 3 "
OPTS+="--stride_frames 1 "
OPTS+="--frameRate 1 "

# audio-related
OPTS+="--audLen 65535 " # 65535
OPTS+="--audRate 11025 " #11025

# learning params
OPTS+="--num_gpus 1 "
OPTS+="--workers 12 "
OPTS+="--batch_size_per_gpu 50 "
OPTS+="--lr_frame 1e-4 "
OPTS+="--lr_sound 1e-4 "
OPTS+="--lr_sound_ground 1e-5 "
OPTS+="--lr_synthesizer 1e-4 "
OPTS+="--lr_grounding 1e-5 "
OPTS+="--num_epoch 1 "
OPTS+="--lr_steps 20 30 "

# display, viz
OPTS+="--disp_iter 20 "
OPTS+="--num_vis 40 "
OPTS+="--num_val 256 "

CUDA_VISIBLE_DEVICES="1" python -u main_col.py $OPTS
