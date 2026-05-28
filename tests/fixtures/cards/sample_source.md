# Mobius — Embedded Vision NPU Compiler

Mobius is a compiler toolkit that takes Pytorch / ONNX models and emits highly
quantized binaries for embedded neural processing units (NPUs) shipped in
automotive, industrial robotics, and AR/VR devices.

## What it does

- INT4 / INT8 quantization-aware compile pipeline with per-channel calibration
- Operator fusion targeting heterogeneous NPU compute clusters
- Static memory layout planning that fits 7B-parameter models into 12 MB SRAM
- Cross-vendor backend: Qualcomm Hexagon, Cadence Tensilica, Synopsys ARC,
  in-house ASICs
- ONNX → vendor IR translation with no runtime overhead

## Who buys it

Engineering leaders at tier-1 automotive suppliers, robotics OEMs, and AR/VR
device makers — anyone shipping ML on a die instead of in the cloud. The wedge
is usually a missed memory budget or a power-watts ceiling that the in-house
team has been bashing their head against for two quarters.

## Signals that a buyer is close

- Public NPU SDK release announcement
- Hiring spree for "embedded ML compiler engineer" or "model quantization"
- ADAS Level 2/3 program milestone slip
- Recent Series-C or strategic auto OEM investment

## Bad fit

- Cloud-only inference shops (no embedded target)
- Pure model-research labs without product hand-off
- Companies that already license a competitor's compiler (lock-in is total)

## Competitors

- Edge Impulse Compiler
- Qualcomm AIMET (vertical lock to Hexagon)
- ONNX Runtime Embedded
