param(
    [string]$TrainingRoot = "D:\FinanceRadarModels",
    [switch]$Install
)

$ErrorActionPreference = "Stop"
$environmentPath = Join-Path $TrainingRoot "envs\qwen-risk-py312"
$pythonPath = Join-Path $environmentPath "Scripts\python.exe"
$cachePath = Join-Path $TrainingRoot "pip-cache"
$temporaryPath = Join-Path $TrainingRoot "tmp"
$modelCachePath = Join-Path $TrainingRoot "huggingface-cache"

New-Item -ItemType Directory -Force -Path $TrainingRoot, $cachePath, $temporaryPath, $modelCachePath | Out-Null
$env:PIP_CACHE_DIR = $cachePath
$env:TEMP = $temporaryPath
$env:TMP = $temporaryPath
$env:HF_HOME = $modelCachePath

if (-not (Test-Path -LiteralPath $pythonPath)) {
    py -3.12 -m venv $environmentPath
}

# Keep the probe and any follow-on plan execution bound to the isolated
# training environment even when the caller did not activate the venv first.
# Without this, Python imports succeed through the explicit interpreter while
# shutil.which("swift") incorrectly reports that the CLI is missing.
$environmentScriptsPath = Join-Path $environmentPath "Scripts"
$env:PATH = "$environmentScriptsPath;$env:PATH"

if ($Install) {
    & $pythonPath -m pip install --upgrade pip
    & $pythonPath -m pip install torch==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128
    & $pythonPath -m pip install ms-swift==4.5.2 bitsandbytes==0.50.1 --extra-index-url https://download.pytorch.org/whl/cu128
}

$probe = @'
import json
import shutil
from dataclasses import fields

result = {
    "python": None,
    "torch_installed": False,
    "torch_version": None,
    "cuda_available": False,
    "cuda_version": None,
    "gpu_name": None,
    "gpu_memory_bytes": None,
    "bitsandbytes_installed": False,
    "bitsandbytes_version": None,
    "swift_executable": shutil.which("swift"),
    "swift_sft_required_fields": {},
    "training_started": False,
}
import sys
result["python"] = sys.version
try:
    import torch
    result["torch_installed"] = True
    result["torch_version"] = torch.__version__
    result["cuda_available"] = torch.cuda.is_available()
    result["cuda_version"] = torch.version.cuda
    if result["cuda_available"]:
        result["gpu_name"] = torch.cuda.get_device_name(0)
        result["gpu_memory_bytes"] = torch.cuda.get_device_properties(0).total_memory
except Exception as exc:
    result["torch_error"] = f"{type(exc).__name__}:{exc}"
try:
    import bitsandbytes
    result["bitsandbytes_installed"] = True
    result["bitsandbytes_version"] = bitsandbytes.__version__
except Exception as exc:
    result["bitsandbytes_error"] = f"{type(exc).__name__}:{exc}"
try:
    from swift.arguments import SftArguments
    available_fields = {item.name for item in fields(SftArguments)}
    required_fields = (
        "val_dataset",
        "quant_method",
        "quant_bits",
        "bnb_4bit_quant_type",
        "bnb_4bit_use_double_quant",
        "target_modules",
        "strict",
    )
    result["swift_sft_required_fields"] = {
        name: name in available_fields for name in required_fields
    }
except Exception as exc:
    result["swift_sft_error"] = f"{type(exc).__name__}:{exc}"
print(json.dumps(result, ensure_ascii=False, indent=2))
'@

$probe | & $pythonPath -
