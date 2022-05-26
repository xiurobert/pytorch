import torch

import torch._prims as prims
import torch._refs as refs
from torch._prims.wrappers import out_wrapper

from torch._prims.utils import (
    DimsType,
    TensorLikeType,
    NumberType,
    corresponding_real_dtype,
    is_float_dtype,
    get_computation_dtype,
    reduction_dtypes,
    REDUCTION_OUTPUT_TYPE_KIND,
)
import torch._refs.linalg as linalg
import torch._refs.linalg.utils

from typing import Optional, List
from functools import partial

__all__ = [
    "vector_norm",
]


@out_wrapper
def vector_norm(
    x: TensorLikeType,
    ord: float = 2.0,
    dim: Optional[DimsType] = None,
    keepdim: bool = False,
    *,
    dtype: Optional[torch.dtype] = None,
):
    # Checks
    linalg.utils.check_fp_or_complex(x, "linalg.vector_norm", half=True)

    if isinstance(dim, int):
        dim = [dim]  # type: ignore[assignment]
    elif not isinstance(dim, List) and dim is not None:
        # refs.sum just accepts List rather than DimType
        dim = list(dim)  # type: ignore[assignment]

    # TODO These things are TORCH_CHECKS, replace them with check(a,b)
    # once https://github.com/pytorch/pytorch/pull/78014 is merged
    if x.numel() == 0 and (ord < 0.0 or ord == float("inf")):
        if dim is None or len(dim) == 0:
            raise RuntimeError(
                "linalg.vector_norm cannot compute the {ord} norm on an empty tensor "
                "because the operation does not have an identity"
            )
        else:
            for d in dim:
                if x.size(d) == 0:
                    raise RuntimeError(
                        f"linalg.vector_norm cannot compute the {ord} norm on the "
                        "dimension {d} because this dimension is empty and the "
                        "operation does not have an identity"
                    )
    linalg.utils.check_norm_dtype(dtype, x.dtype, "linalg.vector_norm")

    computation_dtype, result_dtype = reduction_dtypes(
        x, REDUCTION_OUTPUT_TYPE_KIND.COMPLEX_TO_FLOAT, dtype
    )

    to_result_dtype = partial(prims.convert_element_type, dtype=result_dtype)

    # Implementation
    if ord == 0.0:
        return refs.sum(refs.ne(x, 0.0), dim=dim, keepdim=keepdim, dtype=result_dtype)
    elif ord == float("inf"):
        return to_result_dtype(refs.amax(prims.abs(x), dim=dim, keepdim=keepdim))
    elif ord == float("-inf"):
        return to_result_dtype(refs.amin(prims.abs(x), dim=dim, keepdim=keepdim))
    else:
        # From here on the computation dtype is important as the reduction is non-trivial
        x = prims.convert_element_type(x, computation_dtype)
        reduce_sum = partial(refs.sum, dim=dim, keepdim=keepdim)

        def fast_pow(x, p):
            if p == 1.0:
                return x
            elif p == 2.0:
                return prims.mul(x, x)
            elif p == 0.5:
                return prims.sqrt(x)
            else:
                return prims.pow(x, p)

        # Avoid computing a sqrt in abs and then squaring (more stable)
        # This could potentially be done for complex dtypes as
        # x = prims.real(prims.mul(prims.conj(x), x))
        # and it should be more stable, but it's not clear whether it'll be faster on, say
        # CPU (abs is 1 vectorised operation), so leaving it just for real dtypes for now
        ord_pow = ord
        if ord % 2.0 == 0.0 and is_float_dtype(x.dtype):
            x = prims.mul(x, x)
            ord_pow /= 2.0
        else:
            x = prims.abs(x)
        return to_result_dtype(fast_pow(reduce_sum(fast_pow(x, ord_pow)), 1.0 / ord))
