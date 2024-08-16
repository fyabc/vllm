# coding=utf-8
# Adapted from
# TODO: link to transformers modeling file
# Copyright 2024 The Qwen team.
# Copyright 2023 The vLLM team.
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Inference-only Qwen2-VL model compatible with HuggingFace weights."""
import math
from collections.abc import Mapping
from functools import partial, lru_cache
from typing import Tuple, Optional, List, Iterable, Any, Dict, Type

import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
from einops import rearrange, repeat

from vllm.attention import AttentionMetadata
from vllm.config import MultiModalConfig, CacheConfig
from vllm.distributed import parallel_state
from vllm.distributed import utils as dist_utils
from vllm.inputs import INPUT_REGISTRY, InputContext, LLMInputs
from vllm.logger import init_logger
from vllm.model_executor import SamplingMetadata
from vllm.model_executor.layers.linear import (
    ColumnParallelLinear,
    RowParallelLinear)
from vllm.model_executor.layers.logits_processor import LogitsProcessor
from vllm.model_executor.layers.quantization import QuantizationConfig
from vllm.model_executor.layers.sampler import Sampler
from vllm.model_executor.layers.vocab_parallel_embedding import ParallelLMHead
from vllm.model_executor.model_loader.weight_utils import default_weight_loader
from vllm.model_executor.models.interfaces import SupportsVision
from vllm.model_executor.models.qwen2 import Qwen2Model
from vllm.multimodal import MULTIMODAL_REGISTRY, MultiModalDataDict, MultiModalInputs
from vllm.multimodal.image import cached_get_image_processor
from vllm.sequence import SequenceData, SamplerOutput, IntermediateTensors

from transformers import Qwen2VLConfig
from transformers.models.qwen2_vl.image_processing_qwen2_vl import smart_resize
# from vllm_flash_attn.flash_attn_interface import flash_attn_varlen_func
from flash_attn import flash_attn_varlen_func

logger = init_logger(__name__)


# === Vision Encoder === #


def quick_gelu(x: torch.Tensor, inplace: bool = False) -> torch.Tensor:
    return x * torch.sigmoid(1.702 * x)


class QuickGELU(nn.Module):
    """Applies the Gaussian Error Linear Units function (w/ dummy inplace arg)"""

    def __init__(self, inplace: bool = False) -> None:
        super(QuickGELU, self).__init__()

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        return quick_gelu(input)


class Qwen2VisionMLP(nn.Module):
    def __init__(
        self,
        in_features: int,
        hidden_features: int = None,
        act_layer: Type[nn.Module] = QuickGELU,
        quant_config: Optional[QuantizationConfig] = None,
    ):
        super().__init__()
        self.fc1 = ColumnParallelLinear(in_features, hidden_features, quant_config=quant_config)
        self.act = act_layer()
        self.fc2 = RowParallelLinear(hidden_features, in_features, quant_config=quant_config)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_parallel, _ = self.fc1(x)
        x_parallel = self.act(x_parallel)
        x, _ = self.fc2(x_parallel)
        return x


def rotate_half(x, interleaved=False):
    if not interleaved:
        x1, x2 = x.chunk(2, dim=-1)
        return torch.cat((-x2, x1), dim=-1)
    else:
        x1, x2 = x[..., ::2], x[..., 1::2]
        return rearrange(torch.stack((-x2, x1), dim=-1), "... d two -> ... (d two)", two=2)


def apply_rotary_emb_torch(x, cos, sin, interleaved=False):
    """
    x: (batch_size, seqlen, nheads, headdim)
    cos, sin: (seqlen, rotary_dim / 2) or (batch_size, seqlen, rotary_dim / 2)
    """
    ro_dim = cos.shape[-1] * 2
    assert ro_dim <= x.shape[-1]
    cos = repeat(cos, "... d -> ... 1 (2 d)" if not interleaved else "... d -> ... 1 (d 2)")
    sin = repeat(sin, "... d -> ... 1 (2 d)" if not interleaved else "... d -> ... 1 (d 2)")
    return torch.cat(
        [x[..., :ro_dim] * cos + rotate_half(x[..., :ro_dim], interleaved) * sin, x[..., ro_dim:]],
        dim=-1,
    )


def apply_rotary_pos_emb_vision(t: torch.Tensor, freqs: torch.Tensor) -> torch.Tensor:
    t_ = t.float()
    cos = freqs.cos()
    sin = freqs.sin()
    output = apply_rotary_emb_torch(t_, cos, sin).type_as(t)
    return output


class Qwen2VisionAttention(nn.Module):
    def __init__(
        self,
        embed_dim: Optional[int] = None,
        num_heads: Optional[int] = None,
        projection_size: Optional[int] = None,
        quant_config: Optional[QuantizationConfig] = None,
    ) -> None:
        super().__init__()
        # Per attention head and per partition values.
        world_size = parallel_state.get_tensor_model_parallel_world_size()
        self.hidden_size_per_attention_head = dist_utils.divide(projection_size, num_heads)
        self.num_attention_heads_per_partition = dist_utils.divide(num_heads, world_size)

        self.qkv = ColumnParallelLinear(
            input_size=embed_dim, output_size=3 * projection_size, quant_config=quant_config)
        self.proj = RowParallelLinear(input_size=projection_size, output_size=embed_dim, quant_config=quant_config)

    def forward(
        self,
        x: torch.Tensor,
        cu_seqlens: torch.Tensor,
        rotary_pos_emb: torch.Tensor = None,
    ) -> torch.Tensor:
        # [s, b, c] --> [s, b, head * 3 * head_dim]
        x, _ = self.qkv(x)

        # [s, b, head * 3 * head_dim] --> [s, b, head, 3 * head_dim]
        new_x_shape = x.size()[:-1] + (
            self.num_attention_heads_per_partition,
            3 * self.hidden_size_per_attention_head,
        )
        x = x.view(*new_x_shape)

        # [s, b, head, 3 * head_dim] --> 3 [s, b, head, head_dim]
        q, k, v = dist_utils.split_tensor_along_last_dim(x, 3)
        batch_size = q.shape[1]

        q, k, v = [rearrange(x, 's b ... -> b s ...').contiguous() for x in (q, k, v)]
        if rotary_pos_emb is not None:
            q = apply_rotary_pos_emb_vision(q, rotary_pos_emb)
            k = apply_rotary_pos_emb_vision(k, rotary_pos_emb)
        q, k, v = [rearrange(x, 'b s ... -> (b s) ...') for x in [q, k, v]]

        max_seqlen = (cu_seqlens[1:] - cu_seqlens[:-1]).max().item()
        output = flash_attn_varlen_func(
            q, k, v, cu_seqlens, cu_seqlens, max_seqlen, max_seqlen, 0, causal=False
        )

        context_layer = rearrange(output, '(b s) ... -> b s ...', b=batch_size)
        context_layer = rearrange(context_layer, 'b s h d -> s b (h d)').contiguous()

        output, _ = self.proj(context_layer)
        return output


class Qwen2VisionBlock(nn.Module):
    def __init__(
        self,
        dim: int,
        num_heads: int,
        mlp_ratio: float,
        act_layer: Type[nn.Module] = QuickGELU,
        norm_layer: Type[nn.Module] = partial(nn.LayerNorm, eps=1e-6),
        quant_config: Optional[QuantizationConfig] = None,
    ) -> None:
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)

        self.attn = Qwen2VisionAttention(
            embed_dim=dim,
            num_heads=num_heads,
            projection_size=dim,
            quant_config=quant_config
        )
        self.mlp = Qwen2VisionMLP(dim, mlp_hidden_dim, act_layer=act_layer, quant_config=quant_config)

    def forward(self, x, cu_seqlens, rotary_pos_emb) -> torch.Tensor:
        x = x + self.attn(self.norm1(x), cu_seqlens=cu_seqlens, rotary_pos_emb=rotary_pos_emb)
        x = x + self.mlp(self.norm2(x))
        return x


class Qwen2VisionPatchEmbed(nn.Module):
    def __init__(
        self,
        patch_size: int = 14,
        temporal_patch_size: int = 2,
        in_chans: int = 3,
        embed_dim: int = 1152,
    ) -> None:
        super().__init__()
        self.patch_size = patch_size
        self.temporal_patch_size = temporal_patch_size
        self.embed_dim = embed_dim

        kernel_size = [temporal_patch_size, patch_size, patch_size]
        self.proj = nn.Conv3d(
            in_chans, embed_dim, kernel_size=kernel_size, stride=kernel_size, bias=False
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        L, C = x.shape
        x = x.view(L, -1, self.temporal_patch_size, self.patch_size, self.patch_size)
        x = self.proj(x).view(L, self.embed_dim)
        return x


class Qwen2VisionPatchMerger(nn.Module):
    def __init__(
        self,
        d_model: int,
        context_dim: int,
        norm_layer: Type[nn.Module] = partial(nn.LayerNorm, eps=1e-6),
        spatial_merge_size: int = 2,
        quant_config: Optional[QuantizationConfig] = None,
    ) -> None:
        super().__init__()
        self.hidden_size = context_dim * (spatial_merge_size ** 2)
        self.ln_q = norm_layer(context_dim)
        self.mlp = nn.ModuleList([
            ColumnParallelLinear(self.hidden_size, self.hidden_size, bias=True, quant_config=quant_config),
            nn.GELU(),
            RowParallelLinear(self.hidden_size, d_model, bias=True, quant_config=quant_config),
        ])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.ln_q(x)
        x = x.view(-1, self.hidden_size)

        mlp_fc1, mlp_act, mlp_fc2 = self.mlp
        x_parallel, _ = mlp_fc1(x)
        x_parallel = mlp_act(x_parallel)
        out, _ = mlp_fc2(x_parallel)
        return out


class Qwen2VisionRotaryEmbedding(nn.Module):
    def __init__(self, dim: int, theta: float = 10000.0) -> None:
        super().__init__()
        self.dim = dim
        self.theta = theta
        inv_freq = 1.0 / (theta ** (torch.arange(0, dim, 2, dtype=torch.float) / dim))
        self.register_buffer("inv_freq", inv_freq, persistent=False)
        self._seq_len_cached = 0
        self._freqs_cached = None

    def update_freqs_cache(self, seqlen: int) -> None:
        if seqlen > self._seq_len_cached:
            seqlen *= 2
            self._seq_len_cached = seqlen
            self.inv_freq = 1.0 / (
                self.theta
                ** (
                    torch.arange(0, self.dim, 2, dtype=torch.float, device=self.inv_freq.device)
                    / self.dim
                )
            )
            seq = torch.arange(seqlen, device=self.inv_freq.device, dtype=self.inv_freq.dtype)
            freqs = torch.outer(seq, self.inv_freq)
            self._freqs_cached = freqs

    def forward(self, seqlen: int) -> torch.Tensor:
        self.update_freqs_cache(seqlen)
        return self._freqs_cached[:seqlen]


class Qwen2VisionTransformer(nn.Module):
    def __init__(
        self,
        vision_config: Dict[str, Any],
        norm_eps: float = 1e-6,
        quant_config: Optional[QuantizationConfig] = None,
    ) -> None:
        super().__init__()

        img_size: int = vision_config.get('img_size', 378)
        patch_size: int = vision_config.get('patch_size', 14)
        temporal_patch_size: int = vision_config.get('temporal_patch_size', 2)
        spatial_merge_size: int = vision_config.get('spatial_merge_size', 2)
        in_chans: int = vision_config.get('in_chans', 3)
        hidden_size: int = vision_config.get('hidden_size', 1000)
        embed_dim: int = vision_config.get('embed_dim', 768)
        depth: int = vision_config.get('depth', 12)
        num_heads: int = vision_config.get('num_heads', 16)
        mlp_ratio: float = vision_config.get('mlp_ratio', 4.0)
        pos_type: str = vision_config.get('pos_type', '2drope')

        self.spatial_merge_size = spatial_merge_size
        self.pos_type = pos_type

        self.patch_embed = Qwen2VisionPatchEmbed(
            patch_size=patch_size,
            temporal_patch_size=temporal_patch_size,
            in_chans=in_chans,
            embed_dim=embed_dim,
        )

        norm_layer = partial(nn.LayerNorm, eps=norm_eps)
        if pos_type == 'abs':
            num_patches = (img_size // patch_size) ** 2
            self.pos_embed = nn.Parameter(torch.zeros(num_patches, embed_dim))
            self.norm_pre = norm_layer(embed_dim)
        elif pos_type == '2drope':
            head_dim = embed_dim // num_heads
            self.rotary_pos_emb = Qwen2VisionRotaryEmbedding(head_dim // 2)
        else:
            raise RuntimeError(f"Unsupported pos_type: {pos_type}")

        self.blocks = nn.ModuleList(
            [
                Qwen2VisionBlock(
                    dim=embed_dim,
                    num_heads=num_heads,
                    mlp_ratio=mlp_ratio,
                    norm_layer=norm_layer,
                    quant_config=quant_config,
                )
                for _ in range(depth)
            ]
        )
        self.merger = Qwen2VisionPatchMerger(
            d_model=hidden_size,
            context_dim=embed_dim,
            norm_layer=norm_layer,
            quant_config=quant_config,
        )

    def get_dtype(self) -> torch.dtype:
        return self.blocks[0].mlp.fc2.weight.dtype

    def get_device(self) -> torch.device:
        return self.blocks[0].mlp.fc2.weight.device

    def abs_pos_emb(self, grid_thw):
        pos_embs = []
        src_size = int(math.sqrt(self.pos_embed.size(0)))
        for t, h, w in grid_thw:
            new_pos_emb = F.interpolate(
                self.pos_embed.float().view(1, src_size, src_size, -1).permute(0, 3, 1, 2),
                size=(h, w),
                mode="bicubic",
                align_corners=False,
            ).reshape(
                1, -1,
                h // self.spatial_merge_size, self.spatial_merge_size,
                w // self.spatial_merge_size, self.spatial_merge_size,
            ).expand(t, -1, -1, -1, -1, -1)
            new_pos_emb = (
                new_pos_emb.permute(0, 2, 4, 3, 5, 1).flatten(0, 4).type_as(self.pos_embed)
            )
            pos_embs.append(new_pos_emb)
        pos_embs = torch.cat(pos_embs, dim=0)
        return pos_embs

    def rot_pos_emb(self, grid_thw):
        pos_ids = []
        for t, h, w in grid_thw:
            hpos_ids = torch.arange(h).unsqueeze(1).expand(-1, w)
            wpos_ids = torch.arange(w).unsqueeze(0).expand(h, -1)
            hpos_ids = hpos_ids.reshape(
                h // self.spatial_merge_size, self.spatial_merge_size,
                w // self.spatial_merge_size, self.spatial_merge_size,
            ).permute(0, 2, 1, 3).flatten()
            wpos_ids = wpos_ids.reshape(
                h // self.spatial_merge_size, self.spatial_merge_size,
                w // self.spatial_merge_size, self.spatial_merge_size,
            ).permute(0, 2, 1, 3).flatten()
            pos_ids.append(torch.stack([hpos_ids, wpos_ids], dim=-1).repeat(t, 1))
        pos_ids = torch.cat(pos_ids, dim=0)
        max_grid_size = grid_thw[:, 1:].max()
        rotary_pos_emb_full = self.rotary_pos_emb(max_grid_size)
        rotary_pos_emb = rotary_pos_emb_full[pos_ids].flatten(1)
        return rotary_pos_emb

    def forward(
        self,
        x: torch.Tensor,
        grid_thw: torch.Tensor,
    ) -> torch.Tensor:
        # patchify
        x = x.to(device=self.get_device(), dtype=self.get_dtype())
        x = self.patch_embed(x)

        # compute position embedding
        if self.pos_type == 'abs':
            pos_emb = self.abs_pos_emb(grid_thw)
            x = x + pos_emb
            x = self.norm_pre(x)
            rotary_pos_emb = None
        elif self.pos_type == '2drope':
            rotary_pos_emb = self.rot_pos_emb(grid_thw)
        else:
            raise RuntimeError(f"Unsupported pos_type: {self.pos_type}")

        # compute cu_seqlens
        cu_seqlens = torch.repeat_interleave(
            grid_thw[:, 1] * grid_thw[:, 2], grid_thw[:, 0]
        ).cumsum(dim=0, dtype=torch.int32)
        cu_seqlens = F.pad(cu_seqlens, (1, 0), 'constant', 0)

        # transformers
        x = x.unsqueeze(1)
        for blk in self.blocks:
            x = blk(x, cu_seqlens=cu_seqlens, rotary_pos_emb=rotary_pos_emb)

        # adapter
        x = self.merger(x)
        return x


# === Vision input helpers === #


def get_processor(
    processor_name: str,
    *args,
    trust_remote_code: bool = False,
    **kwargs,
):
    """Gets a processor for the given model name via HuggingFace.

    Derived from `vllm.transformers_utils.image_processor.get_image_processor`.
    """
    # don't put this import at the top level
    # it will call torch.cuda.device_count()
    from transformers import AutoProcessor

    try:
        processor = AutoProcessor.from_pretrained(
            processor_name,
            *args,
            trust_remote_code=trust_remote_code,
            **kwargs)
    except ValueError as e:
        # If the error pertains to the processor class not existing or not
        # currently being imported, suggest using the --trust-remote-code flag.
        # Unlike AutoTokenizer, AutoImageProcessor does not separate such errors
        if not trust_remote_code:
            err_msg = (
                "Failed to load the processor. If the processor is "
                "a custom processor not yet available in the HuggingFace "
                "transformers library, consider setting "
                "`trust_remote_code=True` in LLM or using the "
                "`--trust-remote-code` flag in the CLI.")
            raise RuntimeError(err_msg) from e
        else:
            raise e

    return processor


cached_get_processor = lru_cache(get_processor)

MAX_TEMPORAL_IMAGE_NUM = 10


def input_mapper_for_qwen2_vl(
    ctx: InputContext,
    processed_vision_inputs: Dict[str, Any],
) -> MultiModalInputs:
    """Input mapper for Qwen2-VL. Do nothing since all preprocessing steps already done in input_processor."""
    return MultiModalInputs(processed_vision_inputs)


def _get_max_image_info(image_processor):
    max_resized_height, max_resized_width = smart_resize(
        height=9999999, width=9999999,
        factor=image_processor.patch_size * image_processor.merge_size,
        min_pixels=image_processor.min_pixels,
        max_pixels=image_processor.max_pixels,
    )
    max_grid_t = MAX_TEMPORAL_IMAGE_NUM // image_processor.temporal_patch_size
    max_grid_h = max_resized_height // image_processor.patch_size
    max_grid_w = max_resized_width // image_processor.patch_size
    max_image_tokens = max_grid_t * max_grid_h * max_grid_w
    max_llm_image_tokens = max_image_tokens // image_processor.merge_size // image_processor.merge_size

    return max_resized_height, max_resized_width, max_llm_image_tokens


def get_max_qwen2_vl_image_tokens(ctx: InputContext) -> int:
    image_processor = cached_get_image_processor(ctx.model_config.model)
    max_resized_height, max_resized_width, max_llm_image_tokens = _get_max_image_info(image_processor)
    return max_llm_image_tokens


def dummy_data_for_qwen2_vl(ctx: InputContext, seq_len: int) -> Tuple[SequenceData, Optional[MultiModalDataDict]]:
    image_processor = cached_get_image_processor(ctx.model_config.model)
    max_resized_height, max_resized_width, max_llm_image_tokens = _get_max_image_info(image_processor)

    token_ids = [image_processor.vision_token_id] * max_llm_image_tokens
    token_ids += [0] * (seq_len - max_llm_image_tokens)
    dummy_seqdata = SequenceData(token_ids)
    dummy_image = Image.new("RGB", (max_resized_width, max_resized_height), color=0)

    processed_vision_inputs = image_processor(
        [dummy_image] * MAX_TEMPORAL_IMAGE_NUM,
        vision_infos=None,
        return_tensors="pt",
    )

    return dummy_seqdata, {"image": processed_vision_inputs}


def input_processor_for_qwen2_vl(ctx: InputContext, llm_inputs: LLMInputs) -> LLMInputs:
    # TODO: Refactor code, support multiple type of vision inputs:
    # - str / list[str]: vision url or url list.
    # - PIL.Image / list[PIL.Image]: image object or object list.
    # - dict[str, Any]: image object + parsed vision infos
    #   keys:
    #   - image_objects: list[PIL.Image]
    #   - vision_infos: list[dict]

    multi_modal_data = llm_inputs.get("multi_modal_data")
    if multi_modal_data is None or "image" not in multi_modal_data:
        return llm_inputs

    vision_inputs = multi_modal_data['image']

    if isinstance(vision_inputs, Mapping):
        # dict: image_objects + extra vision_infos
        vision_infos = vision_inputs['vision_infos']
        image_objects = vision_inputs['image_objects']
    else:
        # image_object or list of image_objects
        vision_infos = None
        image_objects = vision_inputs

    if not image_objects:
        return LLMInputs(
            prompt_token_ids=llm_inputs['prompt_token_ids'],
            prompt=llm_inputs['prompt'],
            multi_modal_data=None,
        )

    processor = cached_get_processor(ctx.model_config.model)
    image_processor = processor.image_processor
    vision_token_id = image_processor.vision_token_id

    try:
        processed_vision_inputs = image_processor(image_objects, vision_infos=vision_infos, return_tensors="pt")
    except IndexError:
        # TODO: Remove this debug information for video tasks.
        print(f'[FY DEBUG] Failed to parse {image_objects=} {vision_infos=}')
        raise
    vision_grid_thw = processed_vision_inputs['vision_grid_thw']

    input_ids = llm_inputs['prompt_token_ids']

    new_input_ids = []
    img_num = input_ids.count(vision_token_id)
    assert len(vision_grid_thw) == img_num, \
        f'The text input contains {img_num} image tokens, but {len(vision_grid_thw)} image_objects provided'
    start = 0
    for image_idx in range(img_num):
        end = input_ids.index(vision_token_id, start)
        new_input_ids.extend(input_ids[start:end])

        # Replace <|vision_pad|> with padded vision tokens.
        t, h, w = vision_grid_thw[image_idx]
        llm_grid_t = t
        llm_grid_h = h // image_processor.merge_size
        llm_grid_w = w // image_processor.merge_size
        llm_vision_lens = llm_grid_t * llm_grid_h * llm_grid_w
        new_input_ids.extend([vision_token_id] * llm_vision_lens)
        start = end + 1
    new_input_ids.extend(input_ids[start:])

    return LLMInputs(
        prompt_token_ids=new_input_ids,
        prompt=llm_inputs['prompt'],
        multi_modal_data={'image': processed_vision_inputs} if img_num > 0 else None,
    )


@MULTIMODAL_REGISTRY.register_image_input_mapper(input_mapper_for_qwen2_vl)
@MULTIMODAL_REGISTRY.register_max_image_tokens(get_max_qwen2_vl_image_tokens)
@INPUT_REGISTRY.register_dummy_data(dummy_data_for_qwen2_vl)
@INPUT_REGISTRY.register_input_processor(input_processor_for_qwen2_vl)
class Qwen2VLForConditionalGeneration(nn.Module, SupportsVision):
    def __init__(self,
                 config: Qwen2VLConfig,
                 multimodal_config: MultiModalConfig,
                 cache_config: Optional[CacheConfig] = None,
                 quant_config: Optional[QuantizationConfig] = None) -> None:
        super().__init__()

        self.config = config
        self.multimodal_config = multimodal_config

        self.visual = Qwen2VisionTransformer(
            config.vision_config,
            norm_eps=getattr(config, 'rms_norm_eps', 1e-6),
            quant_config=quant_config,
        )

        self.model = Qwen2Model(config, cache_config, quant_config)

        if config.tie_word_embeddings:
            self.lm_head = self.model.embed_tokens
        else:
            self.lm_head = ParallelLMHead(config.vocab_size,
                                          config.hidden_size,
                                          quant_config=quant_config)

        self.logits_processor = LogitsProcessor(config.vocab_size)
        self.sampler = Sampler()

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
        intermediate_tensors: Optional[IntermediateTensors] = None,
        **kwargs: object,
    ) -> SamplerOutput:
        pixel_values: torch.Tensor = kwargs.get('pixel_values', None)
        vision_grid_thw: torch.Tensor = kwargs.get('vision_grid_thw', None)

        _include_vision = pixel_values is not None and pixel_values.size(0) > 0

        if _include_vision:
            if getattr(self.config, "rope_scaling", {}).get("type", None) == "mrope":
                assert positions.ndim == 2 and positions.size(0) == 3, \
                    f"multimodal section rotary embedding requires (3, seq_len) positions, but got {positions.size()}"

            # compute visual embeddings
            pixel_values = pixel_values.type(self.visual.get_dtype())
            image_embeds = self.visual(pixel_values, vision_grid_thw)

            # compute llm embeddings
            inputs_embeds = self.model.embed_tokens(input_ids)

            # merge llm embeddings and image features
            mask = (input_ids == self.config.vision_token_id)
            inputs_embeds[mask, :] = image_embeds

            input_ids = None
        else:
            inputs_embeds = None

        result = self.model(
            input_ids=input_ids,
            positions=positions,
            kv_caches=kv_caches,
            attn_metadata=attn_metadata,
            inputs_embeds=inputs_embeds,
        )
        return result

    def compute_logits(self, hidden_states: torch.Tensor,
                       sampling_metadata: SamplingMetadata) -> torch.Tensor:
        logits = self.logits_processor(self.lm_head, hidden_states,
                                       sampling_metadata)
        return logits

    def sample(
        self,
        logits: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        next_tokens = self.sampler(logits, sampling_metadata)
        return next_tokens

    def load_weights(self, weights: Iterable[Tuple[str, torch.Tensor]]):
        stacked_params_mapping = [
            # (param_name, shard_name, shard_id)
            ("qkv_proj", "q_proj", "q"),
            ("qkv_proj", "k_proj", "k"),
            ("qkv_proj", "v_proj", "v"),
            ("gate_up_proj", "up_proj", 1),
            ("gate_up_proj", "gate_proj", 0),
        ]
        params_dict = dict(self.named_parameters(remove_duplicate=False))
        for name, loaded_weight in weights:
            if "rotary_emb.inv_freq" in name:
                continue
            if self.config.tie_word_embeddings and "lm_head.weight" in name:
                continue
            for (param_name, weight_name, shard_id) in stacked_params_mapping:
                if weight_name not in name:
                    continue
                name = name.replace(weight_name, param_name)
                param = params_dict[name]
                weight_loader = param.weight_loader
                weight_loader(param, loaded_weight, shard_id)
                break
            else:
                if "visual" in name and "qkv.weight" in name:
                    visual_num_heads = self.config.vision_config["num_heads"]
                    visual_embed_dim = self.config.vision_config["embed_dim"]
                    head_size = visual_embed_dim // visual_num_heads
                    loaded_weight = loaded_weight.view(3, visual_num_heads, head_size, visual_embed_dim)
                    loaded_weight = loaded_weight.transpose(0, 1)
                    loaded_weight = loaded_weight.reshape(-1, visual_embed_dim)
                elif "visual" in name and "qkv.bias" in name:
                    visual_num_heads = self.config.vision_config["num_heads"]
                    visual_embed_dim = self.config.vision_config["embed_dim"]
                    head_size = visual_embed_dim // visual_num_heads
                    loaded_weight = loaded_weight.view(3, visual_num_heads, head_size)
                    loaded_weight = loaded_weight.transpose(0, 1)
                    loaded_weight = loaded_weight.reshape(-1)
                param = params_dict[name]

                weight_loader = getattr(param, "weight_loader", default_weight_loader)
                weight_loader(param, loaded_weight)
