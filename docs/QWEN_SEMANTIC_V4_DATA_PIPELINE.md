# Qwen semantic v4 data pipeline

This pipeline converts an independently adjudicated JSONL into a reproducible
ms-swift SFT corpus. It does **not** run training, read an existing Qwen
prediction, update a production model, or claim that AI adjudications are human
gold labels.

It supports two target contracts:

- `core-v1` is the production-compatible target. The v2 adjudicator's
  `materiality` and `polarity` deterministically derive the existing four fields
  (`materiality`, `polarity`, `adverse_strength`, `semantic_priority`). The four
  mechanism axes, independent impact strength, reason codes, and adjudication
  reason stay in row metadata for audit, but the 1.5B model is not required to
  generate them. `impact_strength` never silently changes legacy
  `adverse_strength`; positive impact strength remains available in audit
  metadata without being mislabeled as adverse strength.
- `full-v2` is the research target and remains the default. The model generates
  materiality, polarity, independent impact strength, four mechanism axes,
  reason codes, and a brief reason.

Use `core-v1` first when the candidate must plug into the current production
contract without changing the serving code.

## Input contract

The preferred production shape is a three-way join:

1. nested output from `scripts/adjudicate_qwen_semantic_multiview_v2.py`;
2. provider input containing only `sample_id` and anonymous `content`;
3. owner-only source index containing identity groups and binding hashes, but
   no content.

```powershell
python scripts/prepare_qwen_semantic_v4_sft.py `
  --adjudications D:\FinanceRadarSecure\qwen-v4\deepseek_multiview_semantic_v2.jsonl `
  --provider-input D:\FinanceRadarSecure\qwen-v4\development_provider_input.jsonl `
  --source-index D:\FinanceRadarSecure\qwen-v4\development_source_index.owner-only.jsonl `
  --target-contract core-v1 `
  --agreement-policy all `
  --output-dir D:\FinanceRadarModels\datasets\qwen-semantic-v4
```

The provider-input row contains the exact anonymous content seen by the
adjudicator:

```json
{
  "sample_id": "stable unique id",
  "content": {
    "as_of": "2026-08-29T00:00:00Z",
    "event_date": "2026-08-29",
    "headline": "source headline",
    "summary": "source summary",
    "passages": [
      {
        "document_type": "8-K",
        "item_section": "8.01",
        "published_at": "2026-08-29",
        "passage": "exact source passage"
      }
    ]
  }
}
```

The real owner-only index contains no `content`:

```json
{
  "sample_id": "stable unique id",
  "source_event_id": "owner-only canonical event id",
  "provider_text_sha256": "sha256 of canonical provider content JSON",
  "source_text_sha256": "sha256 binding of the owner source text",
  "entity_group": "issuer:example",
  "event_chain_group": "chain:example:2026-08"
}
```

The builder recomputes the canonical provider-content hash and requires it to
match, layer by layer, the multiview row's `input_sha256`, the owner index's
`provider_text_sha256`, and its `source_text_sha256`. A legacy two-file mode is
still accepted only when the source index itself contains content and binding
hashes. The nested final v2 target includes materiality, polarity, independent
impact strength, four mechanism axes, stable reason codes, and a concise
reason. The two first-pass views never enter the model prompt or target.

## First-pass agreement filtering

Development rows default to `--agreement-policy all`: both isolated first
passes must agree on materiality, polarity, impact strength, and all four
mechanism axes. Other explicit modes are:

- `core`: require at least materiality and polarity agreement, equivalent to a
  verified `first_pass_pair_agreed=true`;
- `none`: retain every valid adjudication regardless of first-pass agreement.

The manifest records applicable, agreed, kept, and filtered counts. Flat legacy
rows have no independent first passes and are retained as agreement-unavailable.
Fixed external TEST requires `--agreement-policy none`; filtering an external
test by model agreement would create selection bias.

Any prior Qwen/model-prediction field is rejected before the output directory
is created.

## Leakage boundary

Rows are unioned into transitive connected components along three axes:

1. normalized `entity_group`;
2. normalized `event_chain_group`;
3. exact normalized semantic-content hash.

An entire component is assigned to exactly one split. Consequently, if row A
shares an issuer with B and B shares an event chain with C, A/B/C stay in the
same split even when A and C do not directly share an identifier.

The deterministic greedy stratifier targets 70% TRAIN, 15% DEV, and 15% TEST
while minimizing semantic-pair imbalance. The fixed split salt and all input
and output hashes are recorded in the manifest.

## TRAIN-only resampling

Unique TRAIN rows are always written first. A separate effective TRAIN file
uses capped repetition:

- `MATERIAL_ADVERSE`: 3 occurrences;
- `POSITIVE`: 4 occurrences;
- `MIXED`: 4 occurrences;
- combinations use the maximum multiplier, not the product.

DEV and TEST are never repeated. TEST is not supplied to the training command
and is reserved for the final one-time model evaluation.

## Flat input alternative

A pre-joined row is also accepted when it carries `content`, both group keys,
and every v2 final field at the top level. Mixing v1 and v2 targets is rejected.

## Development build commands

The 2026-08-29 owner pool is split across the main development selection and
the additional owner-720 development selection. After a taxonomy-compatible
multiview run has completed, concatenate matching adjudications, provider
inputs, and owner indexes independently, preserving one row per `sample_id`:

```powershell
Set-Location 'D:\FinanceRadarBuilds\ai-adjudicated-risk-router'
$proRoot = 'D:\FinanceRadarSecure\<FRESH_TAXONOMY_COMPATIBLE_PRO_ROOT>'
$selectionRoot = 'D:\FinanceRadarSecure\qwen-v4-selection-20260829-v1'
$mergeRoot = 'D:\FinanceRadarSecure\qwen-v4-pro-merged-core-input-20260829-v2'

$required = @(
  "$proRoot\development\deepseek_multiview_semantic_v2.jsonl",
  "$proRoot\owner720\deepseek_multiview_semantic_v2.jsonl",
  "$selectionRoot\development_provider_input.jsonl",
  "$selectionRoot\owner720_development_provider_input.jsonl",
  "$selectionRoot\development_source_index.owner-only.jsonl",
  "$selectionRoot\owner720_source_index.owner-only.jsonl"
)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missing.Count) { throw "Missing completed input: $($missing -join ', ')" }
New-Item -ItemType Directory -Path $mergeRoot -ErrorAction Stop | Out-Null

Get-Content -LiteralPath $required[0],$required[1] -Encoding UTF8 |
  Set-Content -LiteralPath "$mergeRoot\development_plus_owner720_adjudications.jsonl" -Encoding UTF8
Get-Content -LiteralPath $required[2],$required[3] -Encoding UTF8 |
  Set-Content -LiteralPath "$mergeRoot\development_plus_owner720_provider_input.jsonl" -Encoding UTF8
Get-Content -LiteralPath $required[4],$required[5] -Encoding UTF8 |
  Set-Content -LiteralPath "$mergeRoot\development_plus_owner720_source_index.owner-only.jsonl" -Encoding UTF8

python scripts\prepare_qwen_semantic_v4_sft.py `
  --adjudications "$mergeRoot\development_plus_owner720_adjudications.jsonl" `
  --provider-input "$mergeRoot\development_plus_owner720_provider_input.jsonl" `
  --source-index "$mergeRoot\development_plus_owner720_source_index.owner-only.jsonl" `
  --target-contract core-v1 `
  --agreement-policy all `
  --output-dir 'D:\FinanceRadarModels\datasets\qwen-semantic-v4-pro-core-dev-20260829-v2'
```

Do not point this command at
`qwen-v4-pro-adjudication-20260829-v1`: that run predates the independent
`impact_strength` axis and is therefore not a valid v2 training target. It must
not be retrofitted by inference.

Production-compatible `core-v1` candidate (recommended first):

```powershell
python scripts/prepare_qwen_semantic_v4_sft.py `
  --adjudications D:\FinanceRadarSecure\qwen-v4\ai_adjudications.jsonl `
  --provider-input D:\FinanceRadarSecure\qwen-v4\development_provider_input.jsonl `
  --source-index D:\FinanceRadarSecure\qwen-v4\development_source_index.owner-only.jsonl `
  --target-contract core-v1 `
  --agreement-policy all `
  --output-dir D:\FinanceRadarModels\datasets\qwen-semantic-v4-core
```

Research-only `full-v2` candidate:

```powershell
python scripts/prepare_qwen_semantic_v4_sft.py `
  --adjudications D:\FinanceRadarSecure\qwen-v4\ai_adjudications.jsonl `
  --provider-input D:\FinanceRadarSecure\qwen-v4\development_provider_input.jsonl `
  --source-index D:\FinanceRadarSecure\qwen-v4\development_source_index.owner-only.jsonl `
  --target-contract full-v2 `
  --agreement-policy all `
  --output-dir D:\FinanceRadarModels\datasets\qwen-semantic-v4-full
```

The output directory must not already exist. Output files are installed
atomically and each file receives a SHA-256 sidecar.

## Verified local training environment

The existing isolated training environment was checked on 2026-08-29:

- Python 3.12.2;
- PyTorch 2.11.0+cu128 with CUDA available;
- ms-swift 4.5.2;
- bitsandbytes 0.50.1.

Bootstrap/probe command:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_qwen_training_windows.ps1
```

The generated manifest contains the complete QLoRA ms-swift recipe (LoRA rank,
alpha, optimizer schedule, accumulation, validation cadence, retention, and
strict parsing). Run from the selected dataset directory. Both contracts use
the same command shape because the system prompt and target JSON are already
embedded in each dataset row:

```powershell
Set-Location D:\FinanceRadarModels\datasets\qwen-semantic-v4-core
D:\FinanceRadarModels\envs\qwen-risk-py312\Scripts\swift.exe sft `
  --model Qwen/Qwen2.5-1.5B-Instruct `
  --dataset qwen_risk_sft_train_balanced.jsonl `
  --val_dataset qwen_risk_sft_dev.jsonl `
  --split_dataset_ratio 0 `
  --train_type lora `
  --quant_method bnb `
  --quant_bits 4 `
  --bnb_4bit_quant_type nf4 `
  --bnb_4bit_use_double_quant true `
  --lora_rank 8 `
  --lora_alpha 32 `
  --target_modules all-linear `
  --torch_dtype float16 `
  --num_train_epochs 3 `
  --per_device_train_batch_size 1 `
  --per_device_eval_batch_size 1 `
  --gradient_accumulation_steps 16 `
  --learning_rate 0.0001 `
  --max_length 2048 `
  --eval_steps 20 `
  --save_steps 20 `
  --save_total_limit 2 `
  --logging_steps 5 `
  --warmup_ratio 0.05 `
  --dataloader_num_workers 0 `
  --strict true `
  --output_dir D:\FinanceRadarModels\experiments\qwen-semantic-v4-core
```

For `full-v2`, change the working/output directories to their `-full` paths.
The unique TEST file is deliberately absent from both training commands.

## Strict external fixed TEST

The strict external set must never be repartitioned, filtered, or resampled.
Convert the entire three-way join into one unique TEST file:

```powershell
python scripts/prepare_qwen_semantic_v4_sft.py `
  --adjudications D:\FinanceRadarSecure\qwen-v4\strict_adjudications.jsonl `
  --provider-input D:\FinanceRadarSecure\qwen-v4\strict_provider_input.jsonl `
  --source-index D:\FinanceRadarSecure\qwen-v4\strict_source_index.owner-only.jsonl `
  --target-contract core-v1 `
  --agreement-policy none `
  --fixed-split TEST `
  --output-dir D:\FinanceRadarModels\datasets\qwen-semantic-v4-external-test-core
```

For the frozen 2026-08-29 strict selection, the exact conversion is:

```powershell
Set-Location 'D:\FinanceRadarBuilds\ai-adjudicated-risk-router'
$proRoot = 'D:\FinanceRadarSecure\<FRESH_TAXONOMY_COMPATIBLE_PRO_ROOT>'
$selectionRoot = 'D:\FinanceRadarSecure\qwen-v4-selection-20260829-v1'

python scripts\prepare_qwen_semantic_v4_sft.py `
  --adjudications "$proRoot\strict_external_test\deepseek_multiview_semantic_v2.jsonl" `
  --provider-input "$selectionRoot\strict_external_test_provider_input.jsonl" `
  --source-index "$selectionRoot\strict_external_test_source_index.owner-only.jsonl" `
  --target-contract core-v1 `
  --agreement-policy none `
  --fixed-split TEST `
  --output-dir 'D:\FinanceRadarModels\datasets\qwen-semantic-v4-pro-core-strict-test-20260829-v2'
```

This mode writes `qwen_risk_sft_test.jsonl` and the split audit only. Its
manifest says `EXTERNAL_FIXED_TEST_ONLY`, contains no training recipe, and
sets `training_allowed=false`. Supplying normal split ratios together with
`--fixed-split TEST` is rejected.

The intended sequence is:

1. evaluate the already frozen v3 baseline on this fixed TEST;
2. train/select v4 using development TRAIN/DEV only;
3. freeze the v4 adapter and decoding parameters;
4. evaluate v4 once on the same fixed TEST.

After an adapter and all decoding parameters have been frozen, the reserved
TEST evaluation entry point is:

```powershell
python scripts/evaluate_qwen_semantic_v4_adapter.py `
  --base-model D:\FinanceRadarModels\models\Qwen2.5-1.5B-Instruct `
  --adapter D:\FinanceRadarModels\experiments\qwen-semantic-v4\checkpoint-N `
  --dataset D:\FinanceRadarModels\datasets\qwen-semantic-v4-external-test-core\qwen_risk_sft_test.jsonl `
  --target-contract core-v1 `
  --output-dir D:\FinanceRadarModels\evaluations\qwen-semantic-v4-core-test
```

For a full-v2 dataset/adapter, pass `--target-contract full-v2`. The core-v1
evaluator checks the production four-field contract, materiality/polarity macro
F1, priority recall, and false-priority rate. The full-v2 evaluator additionally
checks every mechanism axis, reason-code micro F1, and `MIXED`/`POSITIVE`
recall. Passing is shadow-only and does not authorize production mutation.
