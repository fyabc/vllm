# SPDX-License-Identifier: Apache-2.0

from typing import (TYPE_CHECKING, ClassVar, Dict, List, Literal, Optional,
                    Protocol, Type, Union, overload, runtime_checkable)

import torch
from torch import Tensor
from typing_extensions import Self, TypeIs

from vllm.logger import init_logger
from vllm.model_executor.layers.quantization.base_config import (
    QuantizationConfig)
from vllm.utils import supports_kw

from .interfaces_base import is_pooling_model

if TYPE_CHECKING:
    from vllm.attention import AttentionMetadata
    from vllm.sequence import IntermediateTensors

logger = init_logger(__name__)

MultiModalEmbeddings = Union[list[Tensor], Tensor, tuple[Tensor, ...]]
"""
The output embeddings must be one of the following formats:

- A list or tuple of 2D tensors, where each tensor corresponds to
    each input multimodal data item (e.g, image).
- A single 3D tensor, with the batch dimension grouping the 2D tensors.
"""


@runtime_checkable
class SupportsMultiModal(Protocol):
    """The interface required for all multi-modal models."""

    supports_multimodal: ClassVar[Literal[True]] = True
    """
    A flag that indicates this model supports multi-modal inputs.

    Note:
        There is no need to redefine this flag if this class is in the
        MRO of your model class.
    """

    def get_multimodal_embeddings(
            self, **kwargs: object) -> Optional[MultiModalEmbeddings]:
        """
        Returns multimodal embeddings generated from multimodal kwargs 
        to be merged with text embeddings.

        Note:
            The returned multimodal embeddings must be in the same order as
            the appearances of their corresponding multimodal data item in the
            input prompt.
        """
        ...

    def get_language_model(self) -> torch.nn.Module:
        """
        Returns the underlying language model used for text generation.

        This is typically the `torch.nn.Module` instance responsible for 
        processing the merged multimodal embeddings and producing hidden states

        Returns:
            torch.nn.Module: The core language model component.
        """
        ...

    # Only for models that support v0 chunked prefill
    # TODO(ywang96): Remove this overload once v0 is deprecated
    @overload
    def get_input_embeddings(
        self,
        input_ids: Tensor,
        multimodal_embeddings: Optional[MultiModalEmbeddings] = None,
        attn_metadata: Optional["AttentionMetadata"] = None,
    ) -> Tensor:
        ...

    @overload
    def get_input_embeddings(
        self,
        input_ids: Tensor,
        multimodal_embeddings: Optional[MultiModalEmbeddings] = None,
    ) -> Tensor:
        """
        Returns the input embeddings merged from the text embeddings from 
        input_ids and the multimodal embeddings generated from multimodal 
        kwargs.
        """
        ...


# We can't use runtime_checkable with ClassVar for issubclass checks
# so we need to treat the class as an instance and use isinstance instead
@runtime_checkable
class _SupportsMultiModalType(Protocol):
    supports_multimodal: Literal[True]


@overload
def supports_multimodal(
        model: Type[object]) -> TypeIs[Type[SupportsMultiModal]]:
    ...


@overload
def supports_multimodal(model: object) -> TypeIs[SupportsMultiModal]:
    ...


def supports_multimodal(
    model: Union[Type[object], object],
) -> Union[TypeIs[Type[SupportsMultiModal]], TypeIs[SupportsMultiModal]]:
    if isinstance(model, type):
        return isinstance(model, _SupportsMultiModalType)

    return isinstance(model, SupportsMultiModal)


@runtime_checkable
class SupportsLoRA(Protocol):
    """The interface required for all models that support LoRA."""

    supports_lora: ClassVar[Literal[True]] = True
    """
    A flag that indicates this model supports LoRA.

    Note:
        There is no need to redefine this flag if this class is in the
        MRO of your model class.
    """
    # The `embedding_module` and `embedding_padding_modules`
    # are empty by default.
    embedding_modules: ClassVar[Dict[str, str]] = {}
    embedding_padding_modules: ClassVar[List[str]] = []
    packed_modules_mapping: ClassVar[Dict[str, List[str]]] = {}


# We can't use runtime_checkable with ClassVar for issubclass checks
# so we need to treat the class as an instance and use isinstance instead
@runtime_checkable
class _SupportsLoRAType(Protocol):
    supports_lora: Literal[True]

    packed_modules_mapping: Dict[str, List[str]]
    embedding_modules: Dict[str, str]
    embedding_padding_modules: List[str]


@overload
def supports_lora(model: Type[object]) -> TypeIs[Type[SupportsLoRA]]:
    ...


@overload
def supports_lora(model: object) -> TypeIs[SupportsLoRA]:
    ...


def supports_lora(
    model: Union[Type[object], object],
) -> Union[TypeIs[Type[SupportsLoRA]], TypeIs[SupportsLoRA]]:
    result = _supports_lora(model)

    if not result:
        lora_attrs = (
            "packed_modules_mapping",
            "embedding_modules",
            "embedding_padding_modules",
        )
        missing_attrs = tuple(attr for attr in lora_attrs
                              if not hasattr(model, attr))

        if getattr(model, "supports_lora", False):
            if missing_attrs:
                logger.warning(
                    "The model (%s) sets `supports_lora=True`, "
                    "but is missing LoRA-specific attributes: %s",
                    model,
                    missing_attrs,
                )
        else:
            if not missing_attrs:
                logger.warning(
                    "The model (%s) contains all LoRA-specific attributes, "
                    "but does not set `supports_lora=True`.", model)

    return result


def _supports_lora(model: Union[Type[object], object]) -> bool:
    if isinstance(model, type):
        return isinstance(model, _SupportsLoRAType)

    return isinstance(model, SupportsLoRA)


@runtime_checkable
class SupportsPP(Protocol):
    """The interface required for all models that support pipeline parallel."""

    supports_pp: ClassVar[Literal[True]] = True
    """
    A flag that indicates this model supports pipeline parallel.

    Note:
        There is no need to redefine this flag if this class is in the
        MRO of your model class.
    """

    def make_empty_intermediate_tensors(
        self,
        batch_size: int,
        dtype: torch.dtype,
        device: torch.device,
    ) -> "IntermediateTensors":
        """Called when PP rank > 0 for profiling purposes."""
        ...

    def forward(
        self,
        *,
        intermediate_tensors: Optional["IntermediateTensors"],
    ) -> Union[Tensor, "IntermediateTensors"]:
        """
        Accept :class:`IntermediateTensors` when PP rank > 0.

        Return :class:`IntermediateTensors` only for the last PP rank.
        """
        ...


# We can't use runtime_checkable with ClassVar for issubclass checks
# so we need to treat the class as an instance and use isinstance instead
@runtime_checkable
class _SupportsPPType(Protocol):
    supports_pp: Literal[True]

    def make_empty_intermediate_tensors(
        self,
        batch_size: int,
        dtype: torch.dtype,
        device: torch.device,
    ) -> "IntermediateTensors":
        ...

    def forward(
        self,
        *,
        intermediate_tensors: Optional["IntermediateTensors"],
    ) -> Union[Tensor, "IntermediateTensors"]:
        ...


@overload
def supports_pp(model: Type[object]) -> TypeIs[Type[SupportsPP]]:
    ...


@overload
def supports_pp(model: object) -> TypeIs[SupportsPP]:
    ...


def supports_pp(
    model: Union[Type[object], object],
) -> Union[bool, TypeIs[Type[SupportsPP]], TypeIs[SupportsPP]]:
    supports_attributes = _supports_pp_attributes(model)
    supports_inspect = _supports_pp_inspect(model)

    if supports_attributes and not supports_inspect:
        logger.warning(
            "The model (%s) sets `supports_pp=True`, but does not accept "
            "`intermediate_tensors` in its `forward` method", model)

    if not supports_attributes:
        pp_attrs = ("make_empty_intermediate_tensors", )
        missing_attrs = tuple(attr for attr in pp_attrs
                              if not hasattr(model, attr))

        if getattr(model, "supports_pp", False):
            if missing_attrs:
                logger.warning(
                    "The model (%s) sets `supports_pp=True`, "
                    "but is missing PP-specific attributes: %s",
                    model,
                    missing_attrs,
                )
        else:
            if not missing_attrs:
                logger.warning(
                    "The model (%s) contains all PP-specific attributes, "
                    "but does not set `supports_pp=True`.", model)

    return supports_attributes and supports_inspect


def _supports_pp_attributes(model: Union[Type[object], object]) -> bool:
    if isinstance(model, type):
        return isinstance(model, _SupportsPPType)

    return isinstance(model, SupportsPP)


def _supports_pp_inspect(model: Union[Type[object], object]) -> bool:
    model_forward = getattr(model, "forward", None)
    if not callable(model_forward):
        return False

    return supports_kw(model_forward, "intermediate_tensors")


@runtime_checkable
class HasInnerState(Protocol):
    """The interface required for all models that has inner state."""

    has_inner_state: ClassVar[Literal[True]] = True
    """
        A flag that indicates this model has inner state.
        Models that has inner state usually need access to the scheduler_config
        for max_num_seqs, etc. True for e.g. both Mamba and Jamba.
    """


@runtime_checkable
class _HasInnerStateType(Protocol):
    has_inner_state: ClassVar[Literal[True]]


@overload
def has_inner_state(model: object) -> TypeIs[HasInnerState]:
    ...


@overload
def has_inner_state(model: Type[object]) -> TypeIs[Type[HasInnerState]]:
    ...


def has_inner_state(
    model: Union[Type[object], object]
) -> Union[TypeIs[Type[HasInnerState]], TypeIs[HasInnerState]]:
    if isinstance(model, type):
        return isinstance(model, _HasInnerStateType)

    return isinstance(model, HasInnerState)


@runtime_checkable
class IsAttentionFree(Protocol):
    """The interface required for all models like Mamba that lack attention,
    but do have state whose size is constant wrt the number of tokens."""

    is_attention_free: ClassVar[Literal[True]] = True
    """
        A flag that indicates this model has no attention.
        Used for block manager and attention backend selection.
        True for Mamba but not Jamba.
    """


@runtime_checkable
class _IsAttentionFreeType(Protocol):
    is_attention_free: ClassVar[Literal[True]]


@overload
def is_attention_free(model: object) -> TypeIs[IsAttentionFree]:
    ...


@overload
def is_attention_free(model: Type[object]) -> TypeIs[Type[IsAttentionFree]]:
    ...


def is_attention_free(
    model: Union[Type[object], object]
) -> Union[TypeIs[Type[IsAttentionFree]], TypeIs[IsAttentionFree]]:
    if isinstance(model, type):
        return isinstance(model, _IsAttentionFreeType)

    return isinstance(model, IsAttentionFree)


@runtime_checkable
class IsHybrid(Protocol):
    """The interface required for all models like Jamba that have both
    attention and mamba blocks, indicates that 
    hf_config has 'layers_block_type'"""

    is_hybrid: ClassVar[Literal[True]] = True
    """
        A flag that indicates this model has both mamba and attention blocks
        , also indicates that the model's hf_config has 
        'layers_block_type' """


@runtime_checkable
class _IsHybridType(Protocol):
    is_hybrid: ClassVar[Literal[True]]


@overload
def is_hybrid(model: object) -> TypeIs[IsHybrid]:
    ...


@overload
def is_hybrid(model: Type[object]) -> TypeIs[Type[IsHybrid]]:
    ...


def is_hybrid(
    model: Union[Type[object], object]
) -> Union[TypeIs[Type[IsHybrid]], TypeIs[IsHybrid]]:
    if isinstance(model, type):
        return isinstance(model, _IsHybridType)

    return isinstance(model, IsHybrid)


@runtime_checkable
class HasNoOps(Protocol):
    has_noops: ClassVar[Literal[True]] = True


@runtime_checkable
class _HasNoOpsType(Protocol):
    has_noops: ClassVar[Literal[True]]


@overload
def has_noops(model: object) -> TypeIs[HasNoOps]:
    ...


@overload
def has_noops(model: Type[object]) -> TypeIs[Type[HasNoOps]]:
    ...


def has_noops(
    model: Union[Type[object], object]
) -> Union[TypeIs[Type[HasNoOps]], TypeIs[HasNoOps]]:
    if isinstance(model, type):
        return isinstance(model, _HasNoOpsType)

    return isinstance(model, HasNoOps)


@runtime_checkable
class SupportsCrossEncoding(Protocol):
    """The interface required for all models that support cross encoding."""

    supports_cross_encoding: ClassVar[Literal[True]] = True


@overload
def supports_cross_encoding(
        model: Type[object]) -> TypeIs[Type[SupportsCrossEncoding]]:
    ...


@overload
def supports_cross_encoding(model: object) -> TypeIs[SupportsCrossEncoding]:
    ...


def _supports_cross_encoding(
    model: Union[Type[object], object],
) -> Union[TypeIs[Type[SupportsCrossEncoding]], TypeIs[SupportsCrossEncoding]]:

    if isinstance(model, type):
        return isinstance(model, SupportsCrossEncoding)

    return isinstance(model, SupportsCrossEncoding)


def supports_cross_encoding(
    model: Union[Type[object], object],
) -> Union[TypeIs[Type[SupportsCrossEncoding]], TypeIs[SupportsCrossEncoding]]:
    return is_pooling_model(model) and _supports_cross_encoding(model)


class SupportsQuant:
    """The interface required for all models that support quantization."""

    packed_modules_mapping: ClassVar[Dict[str, List[str]]] = {}
    quant_config: Optional[QuantizationConfig] = None

    def __new__(cls, *args, **kwargs) -> Self:
        instance = super().__new__(cls)
        quant_config = cls._find_quant_config(*args, **kwargs)
        if quant_config is not None:
            instance.quant_config = quant_config
            instance.quant_config.packed_modules_mapping.update(
                cls.packed_modules_mapping)
        return instance

    @staticmethod
    def _find_quant_config(*args, **kwargs) -> Optional[QuantizationConfig]:
        from vllm.config import VllmConfig  # avoid circular import

        args_values = list(args) + list(kwargs.values())
        for arg in args_values:
            if isinstance(arg, VllmConfig):
                return arg.quant_config

            if isinstance(arg, QuantizationConfig):
                return arg

        return None


@runtime_checkable
class SupportsTranscription(Protocol):
    """The interface required for all models that support transcription."""

    supports_transcription: ClassVar[Literal[True]] = True


@overload
def supports_transcription(
        model: Type[object]) -> TypeIs[Type[SupportsTranscription]]:
    ...


@overload
def supports_transcription(model: object) -> TypeIs[SupportsTranscription]:
    ...


def supports_transcription(
    model: Union[Type[object], object],
) -> Union[TypeIs[Type[SupportsTranscription]], TypeIs[SupportsTranscription]]:
    if isinstance(model, type):
        return isinstance(model, SupportsTranscription)

    return isinstance(model, SupportsTranscription)


@runtime_checkable
class SupportsV0Only(Protocol):
    """Models with this interface are not compatible with V1 vLLM."""

    supports_v0_only: ClassVar[Literal[True]] = True


@overload
def supports_v0_only(model: Type[object]) -> TypeIs[Type[SupportsV0Only]]:
    ...


@overload
def supports_v0_only(model: object) -> TypeIs[SupportsV0Only]:
    ...


def supports_v0_only(
    model: Union[Type[object], object],
) -> Union[TypeIs[Type[SupportsV0Only]], TypeIs[SupportsV0Only]]:
    if isinstance(model, type):
        return isinstance(model, SupportsV0Only)

    return isinstance(model, SupportsV0Only)
