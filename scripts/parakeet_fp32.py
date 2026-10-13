"""Build the GPU's Parakeet model: sherpa-onnx's fp16 export with every weight upcast to fp32.

    python scripts/parakeet_fp32.py <fp16 model dir> <out dir>       (needs `pip install onnx`)

WHY (spec 041, measured 2026-10-09 on JJ's own recorded turns):
  * int8 on CUDA is barely faster than on the CPU (p50 487 vs 469 ms) — the CUDA provider runs no
    int8 integer matmuls, so those nodes go back to the CPU with a copy each way;
  * the published fp16 export is CORRECT on the CPU, but on CUDA it computes in fp16 and 20 of 21
    turns came back empty or `<unk><unk>…` — the overflow the 2026-09-21 research warned about;
  * no fp32 ONNX export is published.
So: take the fp16 export's graph and weights, and make it compute in fp32. Weights rounded to fp16
once lose nothing audible; activations in fp16 are what broke. The encoder is ~2.4 GB in fp32, past
protobuf's 2 GB limit, so it is saved with its weights as external data beside it.

Run with any Python that has `onnx` — it is a one-time tool, not a runtime dependency.
"""
import os
import shutil
import sys

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

FP16, FP32 = TensorProto.FLOAT16, TensorProto.FLOAT


def upcast(model: onnx.ModelProto) -> int:
    g = model.graph
    changed = 0
    for i, init in enumerate(g.initializer):
        if init.data_type == FP16:
            arr = numpy_helper.to_array(init).astype(np.float32)
            g.initializer[i].CopyFrom(numpy_helper.from_array(arr, init.name))
            changed += 1
    for vi in list(g.input) + list(g.output) + list(g.value_info):
        t = vi.type.tensor_type
        if t.elem_type == FP16:
            t.elem_type = FP32
    for node in g.node:
        for a in node.attribute:
            if node.op_type == "Cast" and a.name == "to" and a.i == FP16:
                a.i = FP32
            if a.type == onnx.AttributeProto.TENSOR and a.t.data_type == FP16:
                a.t.CopyFrom(numpy_helper.from_array(numpy_helper.to_array(a.t).astype(np.float32)))
            if a.name in ("dtype",) and a.type == onnx.AttributeProto.INT and a.i == FP16:
                a.i = FP32
    return changed


def main(src: str, dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    for part in ("encoder", "decoder", "joiner"):
        m = onnx.load(os.path.join(src, f"{part}.fp16.onnx"))
        n = upcast(m)
        out = os.path.join(dst, f"{part}.fp32.onnx")
        big = part == "encoder"
        onnx.save_model(m, out, save_as_external_data=big, all_tensors_to_one_file=True,
                        location=f"{part}.fp32.weights" if big else None)
        print(f"{part}: {n} initializers upcast -> {out}")
    shutil.copy(os.path.join(src, "tokens.txt"), os.path.join(dst, "tokens.txt"))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
