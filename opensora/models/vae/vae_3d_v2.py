import functools
from typing import Any, Dict, Tuple, Type, Union, Sequence, Optional
from absl import logging
import torch
import torch.nn as nn 
import numpy as np 
from numpy import typing as nptyping
from opensora.models.vae import model_utils 
from opensora.registry import MODELS
from opensora.utils.ckpt_utils import load_checkpoint
from einops import rearrange, pack, unpack
import torch.nn.functional as F

"""Encoder and Decoder stuctures with 3D CNNs."""

"""
NOTE:
    removed LayerNorm since not used in this arch
    GroupNorm: flax uses default `epsilon=1e-06`, whereas torch uses `eps=1e-05`
    for average pool and upsample, input shape needs to be [N,C,T,H,W] --> if not, adjust the scale factors accordingly

    !!! opensora read video into [B,C,T,H,W] format output

TODO:
    check data dimensions format
"""
def cast_tuple(t, length = 1):
    return t if isinstance(t, tuple) else ((t,) * length)

def divisible_by(num, den):
    return (num % den) == 0

def is_odd(n):
    return not divisible_by(n, 2)

def pad_at_dim(t, pad, dim = -1, value = 0.):
    dims_from_right = (- dim - 1) if dim < 0 else (t.ndim - dim - 1)
    zeros = ((0, 0) * dims_from_right)
    return F.pad(t, (*zeros, *pad), value = value)

def SameConv2d(dim_in, dim_out, kernel_size):
    kernel_size = cast_tuple(kernel_size, 2)
    padding = [k // 2 for k in kernel_size]
    return nn.Conv2d(dim_in, dim_out, kernel_size = kernel_size, padding = padding)

class CausalConv3d(nn.Module):
    def __init__(
        self,
        chan_in,
        chan_out,
        kernel_size: Union[int, Tuple[int, int, int]],
        pad_mode = 'constant',
        strides = None, # allow custom stride
        **kwargs
    ):
        super().__init__()
        kernel_size = cast_tuple(kernel_size, 3)

        time_kernel_size, height_kernel_size, width_kernel_size = kernel_size

        assert is_odd(height_kernel_size) and is_odd(width_kernel_size)

        dilation = kwargs.pop('dilation', 1)
        stride = strides[0] if strides is not None else kwargs.pop('stride', 1)

        self.pad_mode = pad_mode
        time_pad = dilation * (time_kernel_size - 1) + (1 - stride)
        height_pad = height_kernel_size // 2
        width_pad = width_kernel_size // 2

        self.time_pad = time_pad
        self.time_causal_padding = (width_pad, width_pad, height_pad, height_pad, time_pad, 0)

        stride = strides if strides is not None else (stride, 1, 1)
        padding = kwargs.pop('padding', 0)

        if padding == "same" and not all([pad == 1 for pad in padding]):
            padding = "valid"
        dilation = (dilation, 1, 1)
        self.conv = nn.Conv3d(chan_in, chan_out, kernel_size, stride = stride, dilation = dilation, padding=padding, **kwargs)

    def forward(self, x):
        pad_mode = self.pad_mode if self.time_pad < x.shape[2] else 'constant'

        x = F.pad(x, self.time_causal_padding, mode = pad_mode)
        return self.conv(x)


class ResBlock(nn.Module):
    def __init__(
            self, 
            in_channels, # SCH: added
            filters, 
            conv_fn,
            activation_fn=nn.SiLU, 
            use_conv_shortcut=False,
            num_groups=32,
            device="cpu",
            dtype=torch.bfloat16,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.filters = filters
        self.activate = activation_fn()
        self.use_conv_shortcut = use_conv_shortcut
        
        # SCH: MAGVIT uses GroupNorm by default
        self.norm1 = nn.GroupNorm(num_groups, in_channels, device=device, dtype=dtype)
        self.conv1 = conv_fn(in_channels, self.filters, kernel_size=(3, 3, 3), bias=False)
        self.norm2 = nn.GroupNorm(num_groups, self.filters, device=device, dtype=dtype)
        self.conv2 = conv_fn(self.filters, self.filters, kernel_size=(3, 3, 3), bias=False)
        if in_channels != filters:
            if self.use_conv_shortcut:
                self.conv3 = conv_fn(in_channels, self.filters, kernel_size=(3, 3, 3), bias=False) 
            else:
                self.conv3 = conv_fn(in_channels, self.filters, kernel_size=(1, 1, 1), bias=False)


    def forward(self, x):
        # device, dtype = x.device, x.dtype
        # input_dim = x.shape[1]
        residual = x
        x = self.norm1(x)
        x = self.activate(x)
        x = self.conv1(x)
        x = self.norm2(x)
        x = self.activate(x)
        x = self.conv2(x)
        if self.in_channels != self.filters: # SCH: ResBlock X->Y
            residual = self.conv3(residual)
        return x + residual 


class Encoder(nn.Module):
    """Encoder Blocks."""
    def __init__(self, 
        filters = 128,
        num_res_blocks = 4,
        channel_multipliers = (1, 2, 2, 4),
        temporal_downsample = (False, True, True),
        num_groups = 32, # for nn.GroupNorm
        in_out_channels = 3, # SCH: added, in_channels at the start
        latent_embed_dim = 512, # num channels for latent vector
        # conv_downsample = False, 
        custom_conv_padding = None,
        activation_fn = 'swish',
        device="cpu",
        dtype=torch.bfloat16,
    ):
        super().__init__()
        self.filters = filters
        self.num_res_blocks = num_res_blocks
        self.channel_multipliers = channel_multipliers
        self.temporal_downsample = temporal_downsample
        self.num_groups = num_groups
            
        self.embedding_dim = latent_embed_dim
        # self.conv_downsample = conv_downsample
        self.custom_conv_padding = custom_conv_padding

        if activation_fn == 'relu':
            self.activation_fn = nn.ReLU
        elif activation_fn == 'swish':
            self.activation_fn = nn.SiLU
        else:
            raise NotImplementedError
        self.activate = self.activation_fn()

        self.conv_fn = functools.partial(
            CausalConv3d,
            padding='valid' if self.custom_conv_padding is not None else 'same', # SCH: lower letter for pytorch
            dtype=dtype,
            device=device,
        )
        
        self.block_args = dict(
            conv_fn=self.conv_fn,
            dtype=dtype,
            activation_fn=self.activation_fn,
            use_conv_shortcut=False,
            num_groups=self.num_groups,
            device=device,
        )
        
        # NOTE: moved to VAE for separate first frame processing
        # self.conv1 = self.conv_fn(in_out_channels, self.filters, kernel_size=(3, 3, 3), bias=False)

        # ResBlocks and conv downsample
        self.block_res_blocks = []
        self.num_blocks = len(self.channel_multipliers)
        self.conv_blocks = []

        filters = self.filters
        prev_filters = filters # record for in_channels 
        for i in range(self.num_blocks):
            # resblock handling
            filters = self.filters * self.channel_multipliers[i] # SCH: determine the number out_channels
            block_items = []
            for _ in range(self.num_res_blocks):
                block_items.append(ResBlock(prev_filters, filters, **self.block_args))
                prev_filters = filters # update in_channels
            self.block_res_blocks.append(block_items)
            
            if i < self.num_blocks - 1: # SCH: T-Causal Conv 3x3x3, 128->128, stride t x 2 x 2
                t_stride = 2 if self.temporal_downsample[i] else 1
                # TODO: conv_fn usess default stride t x 1 x 1, cannot 
                self.conv_blocks.append(self.conv_fn(prev_filters, filters, kernel_size=(3, 3, 3), strides=(t_stride, 2, 2))) # SCH: should be same in_channel and out_channel
                prev_filters = filters # update in_channels


        # last layer res block
        self.res_blocks = []
        for _ in range(self.num_res_blocks):
            self.res_blocks.append(ResBlock(prev_filters, filters, **self.block_args))
            prev_filters = filters # update in_channels

        # MAGVIT uses Group Normalization
        self.norm1 = nn.GroupNorm(self.num_groups, prev_filters, dtype=dtype, device=device) # SCH: separate <prev_filters> channels into 32 groups

        self.conv2 = nn.Conv3d(prev_filters, self.embedding_dim, kernel_size=(1, 1, 1), dtype=dtype, device=device, padding="same")

    def forward(self, x):
        # dtype, device = x.dtype, x.device

        # NOTE: moved to VAE for separate first frame processing
        # x = self.conv1(x)

        for i in range(self.num_blocks):
            for j in range(self.num_res_blocks):
                x = self.block_res_blocks[i][j](x)

            if i < self.num_blocks - 1:
                x = self.conv_blocks[i](x)
                

        for i in range(self.num_res_blocks):
            x = self.res_blocks[i](x)

        x = self.norm1(x)
        x = self.activate(x)
        x = self.conv2(x)
        return x
    


class Decoder(nn.Module):
    """Decoder Blocks."""
    def __init__(self, 
        latent_embed_dim = 512,
        filters = 128,
        in_out_channels = 4,
        num_res_blocks = 4,
        channel_multipliers = (1, 2, 2, 4),
        temporal_downsample = (False, True, True),
        num_groups = 32, # for nn.GroupNorm
        # upsample = "nearest+conv", # options: "deconv", "nearest+conv"
        custom_conv_padding = None,
        activation_fn = 'swish',
        device="cpu",
        dtype=torch.bfloat16,
    ):
        super().__init__()
        self.output_dim = in_out_channels
        self.embedding_dim = latent_embed_dim
        self.filters = filters
        self.num_res_blocks = num_res_blocks
        self.channel_multipliers = channel_multipliers
        self.temporal_downsample = temporal_downsample
        self.num_groups = num_groups
            
        # self.upsample = upsample
        self.custom_conv_padding = custom_conv_padding
        # self.norm_type = self.config.vqvae.norm_type
        # self.num_remat_block = self.config.vqvae.get('num_dec_remat_blocks', 0)

        if activation_fn == 'relu':
            self.activation_fn = nn.ReLU
        elif activation_fn == 'swish':
            self.activation_fn = nn.SiLU   
        else:
            raise NotImplementedError  
        self.activate = self.activation_fn()

        self.conv_fn = functools.partial(
            CausalConv3d,
            dtype=dtype,
            padding='valid' if self.custom_conv_padding is not None else 'same', # SCH: lower letter for pytorch
            device=device,
        )

        self.block_args = dict(
            conv_fn=self.conv_fn,
            activation_fn=self.activation_fn,
            use_conv_shortcut=False,
            num_groups=self.num_groups,
            device=device,
            dtype=dtype,
        )
        self.num_blocks = len(self.channel_multipliers)

        filters = self.filters * self.channel_multipliers[-1]

        self.conv1 = self.conv_fn(self.embedding_dim, filters, kernel_size=(3, 3, 3), bias=True)

        # last layer res block
        self.res_blocks = []
        for _ in range(self.num_res_blocks):
            self.res_blocks.append(ResBlock(filters, filters, **self.block_args))

        # TODO: do I need to add adaptive GroupNorm in between each block?

        # # NOTE: upsample, dimensions T, H, W
        # self.upsampler_with_t = nn.Upsample(scale_factor=(2,2,2))
        # self.upsampler = nn.Upsample(scale_factor=(1,2,2))

        # ResBlocks and conv upsample
        prev_filters = filters # SCH: in_channels
        self.block_res_blocks = []
        self.num_blocks = len(self.channel_multipliers)
        self.conv_blocks = []
        # SCH: reverse to keep track of the in_channels, but append also in a reverse direction
        for i in reversed(range(self.num_blocks)): 
            filters = self.filters * self.channel_multipliers[i]
            # resblock handling
            block_items = []
            for _ in range(self.num_res_blocks):
                block_items.append(ResBlock(prev_filters, filters, **self.block_args))
                prev_filters = filters # SCH: update in_channels
            self.block_res_blocks.insert(0, block_items) # SCH: append in front
            
            # conv blocks with upsampling
            if i > 0:
                t_stride = 2 if self.temporal_downsample[i - 1] else 1
                # SCH: T-Causal Conv 3x3x3, f -> (t_stride * 2 * 2) * f, depth to space t_stride x 2 x 2
                self.conv_blocks.insert(0, 
                    self.conv_fn(prev_filters, prev_filters * t_stride * 4, kernel_size=(3,3,3))
                )
                
        self.norm1 = nn.GroupNorm(self.num_groups, prev_filters, device=device, dtype=dtype)

        # NOTE: moved to VAE for separate first frame processing
        # self.conv2 = self.conv_fn(prev_filters, self.output_dim, kernel_size=(3, 3, 3))


    def forward(
        self,
        x,
        **kwargs,
    ):
        dtype, device = x.dtype, x.device
        x = self.conv1(x)
        for i in range(self.num_res_blocks):
            x = self.res_blocks[i](x)
        for i in reversed(range(self.num_blocks)): # reverse here to make decoder symmetric with encoder
            for j in range(self.num_res_blocks):
                x = self.block_res_blocks[i][j](x)

            if i > 0:
                t_stride = 2 if self.temporal_downsample[i - 1] else 1
                # SCH: T-Causal Conv 3x3x3, f -> (t_stride * 2 * 2) * f, depth to space t_stride x 2 x 2
                x = self.conv_blocks[i-1](x)
                x = rearrange(x, "B (C ts hs ws) T H W -> B C (T ts) (H hs) (W ws)", ts=t_stride, hs=2, ws=2)


        x = self.norm1(x)
        x = self.activate(x)
        # NOTE: moved to VAE for separate first frame processing
        # x = self.conv2(x) 
        return x
    

@MODELS.register_module()
class VAE_3D_V2(nn.Module):
    """The 3D VAE """
    def __init__(
        self, 
        latent_embed_dim = 256,
        filters = 128,
        num_res_blocks = 2,
        image_size = (128, 128),
        separate_first_frame_encoding = False,
        channel_multipliers = (1, 2, 2, 4),
        temporal_downsample = (True, True, False),
        num_groups = 32, # for nn.GroupNorm
        # conv_downsample = False,
        # upsample = "nearest+conv", # options: "deconv", "nearest+conv"
        custom_conv_padding = None,
        activation_fn = 'swish',
        in_out_channels = 4, 
        kl_embed_dim = 64,
        device="cpu",
        dtype="bf16",
        # precision: Any = jax.lax.Precision.DEFAULT
    ):
        super().__init__()

        if type(dtype) == str:
            if dtype == "bf16":
                dtype = torch.bfloat16
            elif dtype == "fp16":
                dtype = torch.float16
            else:
                raise NotImplementedError(f'dtype: {dtype}')
            

        # ==== Model Params ====
        self.image_size = cast_tuple(image_size, 2)
        self.time_downsample_factor = 2**sum(temporal_downsample)
        self.time_padding = self.time_downsample_factor - 1
        self.separate_first_frame_encoding = separate_first_frame_encoding

        # ==== Model Initialization ====

        # encoder & decoder first and last conv layer
        # SCH: NOTE: following MAGVIT, conv in bias=False in encoder first conv 
        self.conv_in = CausalConv3d(in_out_channels, filters, kernel_size=(3, 3, 3), bias=False, dtype=dtype, device=device)
        self.conv_in_first_frame = nn.Identity()
        self.conv_out_first_frame = nn.Identity()
        if separate_first_frame_encoding:
            self.conv_in_first_frame = SameConv2d(in_out_channels, filters, (3,3))
            self.conv_out_first_frame = SameConv2d(filters, in_out_channels, (3,3))
        self.conv_out = CausalConv3d(filters, in_out_channels, (3,3), dtype=dtype, device=device)


        self.encoder = Encoder(
            filters=filters, 
            num_res_blocks=num_res_blocks, 
            channel_multipliers=channel_multipliers, 
            temporal_downsample=temporal_downsample,
            num_groups = num_groups, # for nn.GroupNorm
            in_out_channels = in_out_channels,
            latent_embed_dim = latent_embed_dim, 
            # conv_downsample = conv_downsample, 
            custom_conv_padding = custom_conv_padding,
            activation_fn = activation_fn, 
            device=device,
            dtype=dtype,
        )
        self.decoder = Decoder(
            latent_embed_dim = latent_embed_dim,
            filters = filters,
            in_out_channels = in_out_channels, 
            num_res_blocks = num_res_blocks,
            channel_multipliers = channel_multipliers,
            temporal_downsample = temporal_downsample,
            num_groups = num_groups, # for nn.GroupNorm
            # upsample = upsample, # options: "deconv", "nearest+conv"
            custom_conv_padding = custom_conv_padding,
            activation_fn = activation_fn,
            device=device,
            dtype=dtype,
        )

        self.quant_conv = nn.Conv3d(latent_embed_dim, 2*kl_embed_dim, 1, device=device, dtype=dtype)
        self.post_quant_conv = nn.Conv3d(kl_embed_dim, latent_embed_dim, 1, device=device, dtype=dtype)
        
        image_down = 2 ** len(temporal_downsample)
        t_down = 2 ** len([x for x in temporal_downsample if x == True])
        self.patch_size = (t_down, image_down, image_down)

    def get_latent_size(self, input_size):
        for i in range(len(input_size)):
            assert input_size[i] % self.patch_size[i] == 0, "Input size must be divisible by patch size"
        input_size = [input_size[i] // self.patch_size[i] for i in range(3)]
        return input_size
    
    def encode(
        self,
        video,
        video_contains_first_frame = True,
    ):
        encode_first_frame_separately = self.separate_first_frame_encoding and video_contains_first_frame

        # whether to pad video or not
        if video_contains_first_frame:
            video_len = video.shape[2]
            video = pad_at_dim(video, (self.time_padding, 0), value = 0., dim = 2)
            video_packed_shape = [torch.Size([self.time_padding]), torch.Size([]), torch.Size([video_len - 1])]

        # NOTE: moved encoder conv1 here for separate first frame encoding
        if encode_first_frame_separately:
            pad, first_frame, video = unpack(video, video_packed_shape, 'b c * h w')
            first_frame = self.conv_in_first_frame(first_frame)
        video = self.conv_in(video)
        if encode_first_frame_separately:
            video, _ = pack([first_frame, video], 'b c * h w')
            video = pad_at_dim(video, (self.time_padding, 0), dim = 2)

        encoded_feature = self.encoder(video)

        moments = self.quant_conv(encoded_feature).to(video.dtype)
        posterior = model_utils.DiagonalGaussianDistribution(moments)
        return posterior
    
    def decode(
        self,
        z,
        video_contains_first_frame = True,
    ):  
        # dtype = z.dtype
        decode_first_frame_separately = self.separate_first_frame_encoding and video_contains_first_frame

        z = self.post_quant_conv(z)
        dec = self.decoder(z)

        # SCH: moved decoder last conv layer here for separate first frame decoding
        if decode_first_frame_separately:
            left_pad, dec_ff, dec = dec[:, :, :self.time_padding], dec[:, :, self.time_padding], dec[:, :, (self.time_padding + 1):]
            out = self.conv_out(dec)
            outff = self.conv_out_first_frame(dec_ff)
            video, _ = pack([outff, out], 'b c * h w')
        else:
            video = self.conv_out(dec)
            # if video were padded, remove padding
            if video_contains_first_frame:
                video = video[:, :, self.time_padding:]

        return video
    
    def forward(
        self,
        input,
        sample_posterior=True,
        video_contains_first_frame = True,
    ):  
        assert input.ndim in {4, 5}, f"received input of {input.ndim} dimensions" # either image or video
        assert input.shape[-2:] == self.image_size, f"received input size {input.shape[-2:]}, but config image size is {self.image_size}"

        is_image = input.ndim == 4

        if is_image:
            video = rearrange(input, 'b c ... -> b c 1 ...')
            video_contains_first_frame = True
        else:
            video = input

        batch, channels, frames = video.shape[:3]
        assert divisible_by(frames - int(video_contains_first_frame), self.time_downsample_factor), f'number of frames {frames} minus the first frame ({frames - int(video_contains_first_frame)}) must be divisible by the total downsample factor across time {self.time_downsample_factor}'

        posterior = self.encode(
            video,
            video_contains_first_frame = video_contains_first_frame,
        )

        if sample_posterior:
            z = posterior.sample()
        else:
            z = posterior.mode()

        recon_video = self.decode(
            z, 
            video_contains_first_frame = video_contains_first_frame
        )

        recon_loss = F.mse_loss(video, recon_video)

        # TODO: add perceptual loss

        # TODO: add adversarial loss

        return recon_loss, recon_video
    

@MODELS.register_module("VAE_MAGVIT_V2")
def VAE_MAGVIT_V2(from_pretrained=None, **kwargs):
    model = VAE_3D_V2(**kwargs)
    if from_pretrained is not None:
        load_checkpoint(model, from_pretrained)
    return model