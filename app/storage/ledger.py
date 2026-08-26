from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.evidence_policy import register_sqlite_integrity_functions


EVIDENCE_SNAPSHOT_SOURCE_IDS = (
    "bls_key_indicators",
    "cftc_enforcement",
    "ecb_press",
    "ecb_statistical_press",
    "eia_press",
    "fda_medwatch",
    "fdic_press_releases",
    "federal_reserve",
    "federal_reserve_press",
    "ftc_press",
    "nvidia_official_news",
    "sec_current_filings",
    "sec_edgar",
    "sec_litigation_releases",
    "sec_trading_suspensions",
    "us_marad",
    "us_treasury",
)


_CURRENT_SOURCE_CONTENT_CTES = """
ranked_source_revisions AS (
    SELECT sr.*,
           ROW_NUMBER() OVER (
               PARTITION BY sr.observation_id
               ORDER BY sr.revision_no DESC
           ) AS source_revision_rank
    FROM source_revisions sr
),
current_source_content AS (
    SELECT r.observation_id,
           r.source_id,
           r.external_id,
           CASE
             WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
             THEN COALESCE(
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.published_at')),''),
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.created_at')),''),
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.posted_at')),''),
               r.source_published_at
             )
             ELSE r.source_published_at
           END AS source_published_at,
           r.local_received_at,
           COALESCE(sr.title,r.title) AS title,
           COALESCE(sr.summary,r.summary) AS summary,
           CASE
             WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
             THEN COALESCE(
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.link')),''),
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.url')),''),
               r.canonical_url
             )
             ELSE r.canonical_url
           END AS canonical_url,
           COALESCE(sr.content_sha256,r.content_sha256) AS content_sha256,
           COALESCE(sr.raw_json,r.raw_json) AS raw_json,
           CASE WHEN sr.revision_kind='delete'
                THEN 'deleted' ELSE r.observation_status END AS observation_status,
           COALESCE(sr.revision_no,0) AS latest_revision_no,
           COALESCE(sr.revision_kind,'new') AS latest_revision_kind,
           COALESCE(sr.revision_at,r.local_received_at) AS latest_revision_at
    FROM raw_observations r
    LEFT JOIN ranked_source_revisions sr
      ON sr.observation_id=r.observation_id
     AND sr.source_revision_rank=1
)
""".strip()


_EVENT_SCOPED_SOURCE_CONTENT_CTES = """
selected_event_evidence AS (
    SELECT * FROM event_evidence WHERE event_id=?
),
selected_evidence_observations AS (
    SELECT DISTINCT observation_id FROM selected_event_evidence
),
ranked_source_revisions AS (
    SELECT sr.*,
           ROW_NUMBER() OVER (
               PARTITION BY sr.observation_id
               ORDER BY sr.revision_no DESC
           ) AS source_revision_rank
    FROM source_revisions sr
    JOIN selected_evidence_observations selected
      ON selected.observation_id=sr.observation_id
),
current_source_content AS (
    SELECT r.observation_id,
           r.source_id,
           r.external_id,
           CASE
             WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
             THEN COALESCE(
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.published_at')),''),
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.created_at')),''),
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.posted_at')),''),
               r.source_published_at
             )
             ELSE r.source_published_at
           END AS source_published_at,
           r.local_received_at,
           COALESCE(sr.title,r.title) AS title,
           COALESCE(sr.summary,r.summary) AS summary,
           CASE
             WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
             THEN COALESCE(
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.link')),''),
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.url')),''),
               r.canonical_url
             )
             ELSE r.canonical_url
           END AS canonical_url,
           COALESCE(sr.content_sha256,r.content_sha256) AS content_sha256,
           COALESCE(sr.raw_json,r.raw_json) AS raw_json,
           CASE WHEN sr.revision_kind='delete'
                THEN 'deleted' ELSE r.observation_status END AS observation_status,
           COALESCE(sr.revision_no,0) AS latest_revision_no,
           COALESCE(sr.revision_kind,'new') AS latest_revision_kind,
           COALESCE(sr.revision_at,r.local_received_at) AS latest_revision_at
    FROM raw_observations r
    JOIN selected_evidence_observations selected
      ON selected.observation_id=r.observation_id
    LEFT JOIN ranked_source_revisions sr
      ON sr.observation_id=r.observation_id
     AND sr.source_revision_rank=1
)
""".strip()


_CAPTURE_INTERPRETATION_CANDIDATE_CTES = """
live_interpretation_capture AS (
    SELECT eo.event_id,r.observation_id,r.source_id,r.external_id,
           r.source_published_at,r.local_received_at,r.title,r.summary,
           r.canonical_url,r.content_sha256,r.raw_json,
           r.observation_status,r.latest_revision_no,r.latest_revision_kind,
           r.latest_revision_at,
           s.authority_tier,
           CASE WHEN x.observation_id IS NOT NULL
                     AND (
                       LOWER(COALESCE(x.last_error,'')) LIKE '%exceeds safe capture limit%'
                       OR LOWER(COALESCE(x.last_error,'')) LIKE '%exceeded safe capture limit%'
                     )
                THEN 1 ELSE 0 END AS oversized_sec_capture
    FROM event_observations eo
    JOIN latest_source_content r ON r.observation_id=eo.observation_id
    JOIN sources s ON s.source_id=r.source_id
    LEFT JOIN sec_filing_enrichments x ON x.observation_id=r.observation_id
    WHERE r.observation_status!='deleted'
),
interpretation_event_bucket AS (
    SELECT ce.event_id,ce.current_version,
           CASE
             WHEN MAX(CASE WHEN c.oversized_sec_capture=1
                                  AND TRIM(COALESCE(c.canonical_url,''))!=''
                            THEN 1 ELSE 0 END)=1
               THEN 'SEC_OVERSIZE_REFETCH_READY'
             WHEN MAX(CASE WHEN TRIM(COALESCE(c.canonical_url,''))!=''
                                  AND (
                                    UPPER(c.authority_tier) IN ('P0','P1')
                                    OR UPPER(c.authority_tier) GLOB 'P0_*'
                                    OR UPPER(c.authority_tier) GLOB 'P1_*'
                                  )
                            THEN 1 ELSE 0 END)=1
               THEN 'OFFICIAL_REFETCH_READY'
             WHEN MAX(CASE WHEN TRIM(COALESCE(c.canonical_url,''))!=''
                            THEN 1 ELSE 0 END)=1
               THEN 'P2_CAPTURE_ONLY'
             ELSE 'NO_URL_RAW_ONLY'
           END AS bucket
    FROM canonical_events ce
    JOIN live_interpretation_capture c ON c.event_id=ce.event_id
    WHERE NOT EXISTS (
      SELECT 1 FROM event_evidence ee WHERE ee.event_id=ce.event_id
    )
    GROUP BY ce.event_id,ce.current_version
),
eligible_interpretation_capture AS (
    SELECT b.bucket,b.current_version,c.*
    FROM interpretation_event_bucket b
    JOIN live_interpretation_capture c ON c.event_id=b.event_id
    WHERE b.bucket IN ('NO_URL_RAW_ONLY','P2_CAPTURE_ONLY')
      AND (
        TRIM(COALESCE(c.title,''))!=''
        OR TRIM(COALESCE(c.summary,''))!=''
      )
)
""".strip()


_DUAL_HUMAN_RECEIPT_MATCH_SQL = """
json_valid(current_version.facts_json)
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.contract_version'
    )='dual-human-selected-evidence-receipt-v2'
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.contract_version'
    )='event-fact-review-v2'
AND json_extract(
      current_version.facts_json,
      '$.human_fact_claim.contract_version'
    )='human-fact-claim-v1'
AND json_sha256(json_remove(
      json_extract(
        current_version.facts_json,
        '$.dual_human_fact_review.selected_evidence_receipt'
      ),
      '$.receipt_sha256'
    ))=LOWER(json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.receipt_sha256'
    ))
AND json_sha256(json_remove(
      json_extract(current_version.facts_json,'$.human_fact_claim'),
      '$.canonical_claim_sha256',
      '$.public_fact_summary',
      '$.public_fact_summary_sha256'
    ))=LOWER(json_extract(
      current_version.facts_json,'$.human_fact_claim.canonical_claim_sha256'
    ))
AND text_sha256(json_extract(
      current_version.facts_json,'$.human_fact_claim.public_fact_summary'
    ))=LOWER(json_extract(
      current_version.facts_json,'$.human_fact_claim.public_fact_summary_sha256'
    ))
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.target_status'
    )='verified'
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_id'
    )=ev.evidence_id
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.event_id'
    )=ev.event_id
AND CAST(json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.event_version'
    ) AS INTEGER)=rel.event_version
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.evidence_id'
    )=ev.evidence_id
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.evidence_status_after'
    )=ev.evidence_status
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.evidence_url'
    )=TRIM(ev.evidence_url)
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.evidence_passage'
    )=ev.evidence_passage
AND LOWER(json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.source_content_sha256'
    ))=LOWER(ro.content_sha256)
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.source_id'
    )=ro.source_id
AND UPPER(json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.source_authority_tier'
    ))=UPPER(src.authority_tier)
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.source_observation_status'
    )=ro.observation_status
AND CAST(json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.source_revision_no'
    ) AS INTEGER)=ro.latest_revision_no
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.source_revision_kind'
    )=ro.latest_revision_kind
AND CAST(json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.source_passage_currently_proven'
    ) AS INTEGER)=CASE
      WHEN ro.latest_revision_kind NOT IN ('edit','delete') THEN 1
      WHEN INSTR(
        COALESCE(ro.title,'') || CHAR(10) || COALESCE(ro.summary,'') || CHAR(10) ||
        COALESCE(ro.raw_json,''),TRIM(ev.evidence_passage)
      )>0 THEN 1 ELSE 0 END
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.evidence_fingerprint_before'
    )=rel.evidence_fingerprint
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.canonical_claim_sha256'
    )=json_extract(
      current_version.facts_json,
      '$.human_fact_claim.canonical_claim_sha256'
    )
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.canonical_claim_sha256'
    )=json_extract(
      current_version.facts_json,
      '$.human_fact_claim.canonical_claim_sha256'
    )
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.public_fact_summary_sha256'
    )=json_extract(
      current_version.facts_json,
      '$.human_fact_claim.public_fact_summary_sha256'
    )
AND json_extract(
      current_version.facts_json,
      '$.dual_human_fact_review.selected_evidence_receipt.public_fact_summary_sha256'
    )=json_extract(
      current_version.facts_json,
      '$.human_fact_claim.public_fact_summary_sha256'
    )
AND json_extract(
      current_version.facts_json,'$.public_fact_summary'
    )=json_extract(
      current_version.facts_json,'$.human_fact_claim.public_fact_summary'
    )
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.predicate'
    )=ce.event_type
AND json_extract(
      current_version.facts_json,'$.claim_subject'
    )=json_extract(
      current_version.facts_json,'$.human_fact_claim.subject'
    )
AND json_extract(
      current_version.facts_json,'$.claim_action'
    )=json_extract(
      current_version.facts_json,'$.human_fact_claim.action_quote'
    )
AND json_extract(
      current_version.facts_json,'$.claim_stage'
    )=json_extract(
      current_version.facts_json,'$.human_fact_claim.stage'
    )
AND UPPER(json_extract(
      current_version.facts_json,'$.human_fact_claim.stage'
    )) IN ('FILED','DISCLOSED','EFFECTIVE','ONGOING','COMPLETED')
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.modality'
    )='REALIZED'
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.subject'
    ) IN (TRIM(ce.company_name),TRIM(ce.ticker_at_event))
AND INSTR(
      ev.evidence_passage,
      json_extract(current_version.facts_json,'$.human_fact_claim.action_quote')
    )>0
AND INSTR(
      ev.evidence_passage,
      json_extract(current_version.facts_json,'$.human_fact_claim.fact_sentence_quote')
    )>0
AND INSTR(
      json_extract(current_version.facts_json,'$.human_fact_claim.fact_sentence_quote'),
      json_extract(current_version.facts_json,'$.human_fact_claim.action_quote')
    )>0
AND (
      LENGTH(json_extract(
        current_version.facts_json,'$.human_fact_claim.fact_sentence_quote'
      ))-LENGTH(REPLACE(
        json_extract(current_version.facts_json,'$.human_fact_claim.fact_sentence_quote'),
        json_extract(current_version.facts_json,'$.human_fact_claim.action_quote'),
        ''
      ))
    )/LENGTH(json_extract(
      current_version.facts_json,'$.human_fact_claim.action_quote'
    ))=1
AND (
      COALESCE(json_extract(
        current_version.facts_json,'$.human_fact_claim.object_quote'
      ),'')=''
      OR (
        INSTR(ev.evidence_passage,json_extract(
          current_version.facts_json,'$.human_fact_claim.object_quote'
        ))>0
        AND INSTR(json_extract(
          current_version.facts_json,'$.human_fact_claim.fact_sentence_quote'
        ),json_extract(
          current_version.facts_json,'$.human_fact_claim.object_quote'
        ))>0
      )
    )
AND (
      COALESCE(json_extract(
        current_version.facts_json,'$.human_fact_claim.event_date_or_effective_date'
      ),'')=''
      OR INSTR(ev.evidence_passage,json_extract(
        current_version.facts_json,'$.human_fact_claim.event_date_or_effective_date'
      ))>0
    )
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_basis'
    )='EXACT_IN_PASSAGE'
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_action_binding_contract'
    )='minimal-subject-action-clause-v1'
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.realized_language_gate_contract'
    )='realized-language-fail-closed-v1'
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.realized_action_head_contract'
    )='realized-action-head-allowlist-v1'
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.fact_predicate_contract'
    )='human-fact-predicate-map-v1'
AND human_fact_predicate_compatible(
      json_extract(current_version.facts_json,'$.human_fact_claim.fact_predicate'),
      ce.event_type,
      ce.event_family,
      json_extract(current_version.facts_json,'$.human_fact_claim.action_quote'),
      json_extract(current_version.facts_json,'$.human_fact_claim.object_quote')
    )=1
AND text_sha256(ev.evidence_passage)=LOWER(json_extract(
      current_version.facts_json,'$.human_fact_claim.evidence_passage_sha256'
    ))
AND fact_quote_context_valid(
      ev.evidence_passage,
      json_extract(current_version.facts_json,'$.human_fact_claim.fact_sentence_quote'),
      json_extract(current_version.facts_json,'$.human_fact_claim.fact_sentence_start'),
      json_extract(current_version.facts_json,'$.human_fact_claim.fact_sentence_end')
    )=1
AND realized_claim_language_safe(
      json_extract(current_version.facts_json,'$.human_fact_claim.action_quote'),
      json_extract(current_version.facts_json,'$.human_fact_claim.fact_sentence_quote'),
      json_extract(current_version.facts_json,'$.human_fact_claim.subject_surface_quote'),
      ev.evidence_passage
    )=1
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_surface_quote'
    ) IN (
      json_extract(current_version.facts_json,'$.human_fact_claim.subject'),
      '$' || json_extract(current_version.facts_json,'$.human_fact_claim.subject')
    )
AND LENGTH(json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_action_gap_quote'
    ))>0
AND SUBSTR(json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_action_gap_quote'
    ),1,1) NOT GLOB '[A-Za-z0-9]'
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_action_prefix_quote'
    )=json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_surface_quote'
    ) || json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_action_gap_quote'
    ) || json_extract(
      current_version.facts_json,'$.human_fact_claim.action_quote'
    )
AND SUBSTR(
      LTRIM(
        json_extract(current_version.facts_json,'$.human_fact_claim.fact_sentence_quote'),
        ' ' || CHAR(9) || CHAR(10) || CHAR(13)
      ),
      1,
      LENGTH(json_extract(
        current_version.facts_json,'$.human_fact_claim.subject_action_prefix_quote'
      ))
    )=json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_action_prefix_quote'
    )
AND LOWER(TRIM(
      json_extract(
        current_version.facts_json,'$.human_fact_claim.subject_action_gap_quote'
      ),
      ' ' || CHAR(9) || CHAR(10) || CHAR(13) || ',;:()[]-–—'
    ))=json_extract(
      current_version.facts_json,'$.human_fact_claim.subject_action_gap_normalized'
    )
AND (
      json_extract(
        current_version.facts_json,'$.human_fact_claim.subject_action_gap_normalized'
      )=''
      OR json_extract(
        current_version.facts_json,'$.human_fact_claim.subject_action_gap_normalized'
      ) IN (
        'has','had','have','is','was','were','are','did','does','do',
        'formally','officially','successfully','voluntarily','immediately','now','also'
      )
      OR (
        SUBSTR(
          json_extract(
            current_version.facts_json,'$.human_fact_claim.subject_action_gap_normalized'
          ),
          1,
          INSTR(json_extract(
            current_version.facts_json,'$.human_fact_claim.subject_action_gap_normalized'
          ),' ')-1
        ) IN ('has','had','have','is','was','were','are','did','does','do')
        AND SUBSTR(
          json_extract(
            current_version.facts_json,'$.human_fact_claim.subject_action_gap_normalized'
          ),
          INSTR(json_extract(
            current_version.facts_json,'$.human_fact_claim.subject_action_gap_normalized'
          ),' ')+1
        ) IN ('formally','officially','successfully','voluntarily','immediately','now','also')
        AND INSTR(SUBSTR(
          json_extract(
            current_version.facts_json,'$.human_fact_claim.subject_action_gap_normalized'
          ),
          INSTR(json_extract(
            current_version.facts_json,'$.human_fact_claim.subject_action_gap_normalized'
          ),' ')+1
        ),' ')=0
      )
    )
AND json_extract(
      current_version.facts_json,'$.human_fact_claim.public_fact_summary'
    )=json_extract(
      current_version.facts_json,'$.human_fact_claim.subject'
    ) || '：' || json_extract(
      current_version.facts_json,'$.human_fact_claim.fact_sentence_quote'
    )
AND rel.contract_version='event-fact-review-v2'
""".strip()


_CURRENT_EVIDENCE_PASSAGE_MATCH_SQL = """
ro.observation_status!='deleted'
AND (
  ro.latest_revision_kind NOT IN ('edit','delete')
  OR INSTR(
       COALESCE(ro.title,'') || CHAR(10) ||
       COALESCE(ro.summary,'') || CHAR(10) ||
       COALESCE(ro.raw_json,''),
       TRIM(ev.evidence_passage)
     )>0
)
""".strip()


_SEC_CURRENT_SUPPORTED_FACT_SLOT_CTE = """
sec_current_supported_fact_slots AS (
    SELECT current_version.event_id,current_version.version
    FROM event_versions current_version
    JOIN canonical_events slot_event
      ON slot_event.event_id=current_version.event_id
     AND slot_event.current_version=current_version.version
    JOIN event_evidence slot_evidence
      ON slot_evidence.event_id=current_version.event_id
     AND slot_evidence.evidence_id=json_extract(
           current_version.facts_json,'$.evidence_id'
         )
    JOIN json_each(
      CASE WHEN json_valid(current_version.facts_json)
           THEN current_version.facts_json ELSE '{}' END,
      '$.claim_fact_slots.facts'
    ) slot
    WHERE LENGTH(TRIM(COALESCE(json_extract(slot.value,'$.predicate'),'')))>0
      AND CAST(json_extract(slot.value,'$.event_type_compatible') AS INTEGER)=1
      AND LENGTH(TRIM(COALESCE(json_extract(slot.value,'$.action_text'),'')))>0
      AND LENGTH(TRIM(COALESCE(json_extract(slot.value,'$.object'),'')))>0
      AND LENGTH(TRIM(COALESCE(json_extract(slot.value,'$.evidence_sentence'),'')))>=20
      AND LOWER(json_extract(
            current_version.facts_json,'$.claim_fact_slots.event_type'
          ))=LOWER(TRIM(slot_event.event_type))
      AND INSTR(slot_evidence.evidence_passage,json_extract(
            slot.value,'$.evidence_sentence'
          ))>0
      AND INSTR(json_extract(
            current_version.facts_json,'$.public_fact_summary'
          ),json_extract(slot.value,'$.action_text'))>0
      AND INSTR(json_extract(
            current_version.facts_json,'$.public_fact_summary'
          ),json_extract(slot.value,'$.object'))>0
      AND (
        (
          json_extract(slot.value,'$.subject_binding') IN (
            'EXPLICIT_ISSUER','EXPLICIT_ISSUER_CONTEXT'
          )
          AND CAST(json_extract(
                slot.value,'$.issuer_name_explicit_in_passage'
              ) AS INTEGER)=1
          AND LOWER(TRIM(json_extract(slot.value,'$.subject')))=LOWER(TRIM(
                json_extract(current_version.facts_json,'$.claim_subject')
              ))
        )
      )
    GROUP BY current_version.event_id,current_version.version
)
""".strip()

_EVENT_SCOPED_SEC_CURRENT_SUPPORTED_FACT_SLOT_CTE = (
    _SEC_CURRENT_SUPPORTED_FACT_SLOT_CTE.replace(
        "JOIN event_evidence slot_evidence",
        "JOIN selected_event_evidence slot_evidence",
    )
)


_SEC_CURRENT_FACT_SLOT_MATCH_SQL = """
json_valid(current_version.facts_json)
AND json_extract(
      current_version.facts_json,
      '$.admission_contract_version'
    )='event-admission-v3'
AND json_extract(
      current_version.facts_json,
      '$.fact_slot_contract_version'
    )='deterministic-evidence-fact-slots-v2'
AND json_extract(
      current_version.facts_json,
      '$.claim_fact_slots.contract_version'
    )='deterministic-evidence-fact-slots-v2'
AND LOWER(json_extract(
      current_version.facts_json,
      '$.claim_fact_slots.event_type'
    ))=LOWER(TRIM(ce.event_type))
AND LOWER(json_extract(
      current_version.facts_json,
      '$.claim_action'
    ))=LOWER(TRIM(ce.event_type))
AND LENGTH(json_extract(
      current_version.facts_json,
      '$.claim_fact_slots.passage_sha256'
    ))=64
AND LENGTH(json_extract(
      current_version.facts_json,
      '$.claim_fact_slots.canonical_passage_sha256'
    ))=64
AND CAST(json_extract(
      current_version.facts_json,
      '$.claim_fact_slots.compatible_fact_count'
    ) AS INTEGER)>0
AND LENGTH(json_extract(
      current_version.facts_json,
      '$.fact_slot_receipt_sha256'
    ))=64
AND json_extract(current_version.facts_json,'$.evidence_id')=ev.evidence_id
AND json_extract(
      current_version.facts_json,
      '$.source_observation_id'
    )=ev.observation_id
AND LOWER(json_extract(
      current_version.facts_json,
      '$.source_content_sha256'
    ))=LOWER(ro.content_sha256)
AND ro.observation_status!='deleted'
AND sec_slot.event_id IS NOT NULL
""".strip()


PUBLIC_EVENT_STATE_CTE = f"""
WITH {_CURRENT_SOURCE_CONTENT_CTES},
{_SEC_CURRENT_SUPPORTED_FACT_SLOT_CTE},
ranked_rough_reviews AS (
    SELECT job_id,event_id,payload_json,updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY event_id
               ORDER BY updated_at DESC,job_id DESC
           ) AS rough_rank
    FROM pipeline_jobs
    WHERE status='COMPLETED_AUTHORIZED_ROUGH_REVIEW'
),
ranked_light_followups AS (
    SELECT job_id,event_id,status,payload_json,updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY event_id
               ORDER BY updated_at DESC,job_id DESC
           ) AS followup_rank
    FROM pipeline_jobs
    WHERE job_type='light_verification_followup'
      AND status IN ('PENDING_EVIDENCE_REVIEW','PENDING_HUMAN_REVIEW')
),
event_reader_evidence AS (
    SELECT ev.event_id,
           COUNT(*) AS citable_evidence_count
    FROM event_evidence ev
    JOIN canonical_events ce ON ce.event_id=ev.event_id
    JOIN event_evidence_relations rel
      ON rel.event_id=ev.event_id
     AND rel.evidence_id=ev.evidence_id
     AND rel.event_version=ce.current_version
    JOIN current_source_content ro ON ro.observation_id=ev.observation_id
    JOIN sources src ON src.source_id=ro.source_id
    JOIN event_versions current_version
      ON current_version.event_id=ce.event_id
     AND current_version.version=ce.current_version
    LEFT JOIN sec_current_supported_fact_slots sec_slot
      ON sec_slot.event_id=ce.event_id
     AND sec_slot.version=ce.current_version
    WHERE TRIM(COALESCE(ev.evidence_url,''))!=''
      AND LENGTH(TRIM(COALESCE(ev.evidence_passage,'')))>=40
      AND (
        (
          ev.evidence_status IN (
            'machine_extracted_unreviewed','candidate_passage',
            'confirmed_primary','accepted_manual_primary_evidence',
            'accepted_light_primary_evidence'
          )
          AND rel.relation_status IN ('SCOPED_MATCH','HUMAN_CONFIRMED')
          AND {_SEC_CURRENT_FACT_SLOT_MATCH_SQL}
          AND rel.contract_version='event-admission-v3'
          AND rel.evidence_fingerprint=json_extract(
                current_version.facts_json,'$.evidence_fingerprint'
              )
        ) OR (
          ev.evidence_status='accepted_dual_human_primary_evidence'
          AND rel.relation_status='HUMAN_CONFIRMED'
          AND {_DUAL_HUMAN_RECEIPT_MATCH_SQL}
        )
      )
      AND rel.subject_match=1
      AND rel.event_claim_supported=1
      AND rel.date_coherent=1
      AND {_CURRENT_EVIDENCE_PASSAGE_MATCH_SQL}
      AND (
        UPPER(src.authority_tier) IN ('P0','P1')
        OR UPPER(src.authority_tier) GLOB 'P0_*'
        OR UPPER(src.authority_tier) GLOB 'P1_*'
      )
    GROUP BY ev.event_id
),
event_public AS (
    SELECT canonical.*,
           light.status AS light_followup_status,
           light.updated_at AS light_followup_updated_at,
           CASE WHEN json_valid(light.payload_json)
             THEN json_extract(light.payload_json,'$.light_verification_followup.expected_next_action')
           END AS light_followup_next_action,
           CASE
             WHEN canonical.status='rejected' THEN 'excluded'
             WHEN light.job_id IS NOT NULL AND canonical.status!='weak' THEN 'pending_verification'
             WHEN canonical.status='verified' THEN 'verified'
             WHEN canonical.status='weak' OR (
               rough.job_id IS NOT NULL
               AND CASE WHEN json_valid(rough.payload_json)
                 THEN COALESCE(
                   json_extract(rough.payload_json,'$.rough_review.outcome'),
                   CASE WHEN UPPER(COALESCE(
                     json_extract(rough.payload_json,'$.rough_review.decision_status'),''
                   ))='INSUFFICIENT' THEN 'ROUGH_INSUFFICIENT' END
                 )
               END='ROUGH_INSUFFICIENT'
             ) THEN 'insufficient'
             WHEN canonical.status='candidate' AND rough.job_id IS NOT NULL THEN 'rough_reviewed'
             ELSE 'pending_verification'
           END AS public_state,
           COALESCE(
             CASE WHEN json_valid(rough.payload_json)
               THEN json_extract(rough.payload_json,'$.rough_review.reviewed_at')
             END,
              rough.updated_at
           ) AS reviewed_at,
           COALESCE(reader_evidence.citable_evidence_count,0) AS citable_evidence_count,
           CASE WHEN json_valid(current_version.facts_json)
             THEN NULLIF(TRIM(json_extract(
               current_version.facts_json,'$.public_fact_summary'
             )),'')
           END AS public_fact_summary,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.claim_subject')
            END AS claim_subject,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.claim_action')
            END AS claim_action,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.claim_stage')
            END AS claim_stage,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.known_at')
            END AS known_at,
           CASE WHEN COALESCE(
             NULLIF(TRIM(canonical.company_name),''),
             NULLIF(TRIM(canonical.ticker_at_event),''),
             ''
           )!=''
             THEN 1 ELSE 0
           END AS reader_has_subject,
           CASE WHEN json_valid(current_version.facts_json) AND LENGTH(COALESCE(
             NULLIF(TRIM(json_extract(current_version.facts_json,'$.public_fact_summary')),''),
             ''
            ))>=20
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_subject'),''
              )))>=2
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_action'),''
              )))>=3
              AND UPPER(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_stage'),''
              ))) IN ('PROPOSED','FILED','DISCLOSED','EFFECTIVE','ONGOING','COMPLETED')
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.known_at'),''
              )))>=20
              THEN 1 ELSE 0 END AS reader_has_fact_summary,
           CASE WHEN
             COALESCE(
               NULLIF(TRIM(canonical.company_name),''),
               NULLIF(TRIM(canonical.ticker_at_event),''),
               ''
             )!=''
             AND COALESCE(reader_evidence.citable_evidence_count,0)>0
             AND json_valid(current_version.facts_json)
              AND LENGTH(COALESCE(
               NULLIF(TRIM(json_extract(current_version.facts_json,'$.public_fact_summary')),''),
               ''
              ))>=20
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_subject'),''
              )))>=2
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_action'),''
              )))>=3
              AND UPPER(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_stage'),''
              ))) IN ('PROPOSED','FILED','DISCLOSED','EFFECTIVE','ONGOING','COMPLETED')
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.known_at'),''
              )))>=20
              THEN 1 ELSE 0
           END AS reader_ready
    FROM canonical_events canonical
    LEFT JOIN ranked_rough_reviews rough
      ON rough.event_id=canonical.event_id AND rough.rough_rank=1
    LEFT JOIN ranked_light_followups light
      ON light.event_id=canonical.event_id AND light.followup_rank=1
    LEFT JOIN event_reader_evidence reader_evidence
      ON reader_evidence.event_id=canonical.event_id
    LEFT JOIN event_versions current_version
      ON current_version.event_id=canonical.event_id
     AND current_version.version=canonical.current_version
)
""".strip()


def _page_scoped_public_event_state_cte(
    *,
    where_sql: str,
    sort_sql: str,
    source_excerpt_chars: int | None = None,
) -> str:
    """Build the public event projection after bounding work to one page.

    ``PUBLIC_EVENT_STATE_CTE`` intentionally remains the authoritative full-ledger
    query for reviewer-only ``reader_ready`` filtering.  Public browsing does not
    filter on that derived field, so evaluating its evidence-integrity contract for
    every canonical event before ``LIMIT`` is wasted work.  This equivalent
    projection first selects the canonical page and then evaluates source revisions,
    fact slots and citable evidence only for those bounded event ids.
    """

    excerpt_limit = (
        max(64, min(int(source_excerpt_chars), 4096))
        if source_excerpt_chars is not None
        else None
    )
    source_title_sql = "COALESCE(revision.title,raw.title)"
    source_summary_sql = "COALESCE(revision.summary,raw.summary)"
    if excerpt_limit is not None:
        source_title_sql = f"SUBSTR({source_title_sql},1,{excerpt_limit})"
        source_summary_sql = f"SUBSTR({source_summary_sql},1,{excerpt_limit})"

    return f"""
WITH paged_canonical AS (
    SELECT e.*
    FROM canonical_events e
    {where_sql}
    ORDER BY {sort_sql}
    LIMIT ? OFFSET ?
),
page_evidence_observations AS (
    SELECT DISTINCT ev.observation_id
    FROM paged_canonical page
    CROSS JOIN event_evidence ev
    WHERE ev.event_id=page.event_id
),
ranked_source_revisions AS (
    SELECT sr.*,
           ROW_NUMBER() OVER (
               PARTITION BY sr.observation_id
               ORDER BY sr.revision_no DESC
           ) AS source_revision_rank
    FROM page_evidence_observations page_observation
    CROSS JOIN source_revisions sr
    WHERE sr.observation_id=page_observation.observation_id
),
current_source_content AS (
    SELECT r.observation_id,
           r.source_id,
           r.external_id,
           CASE
             WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
             THEN COALESCE(
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.published_at')),''),
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.created_at')),''),
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.posted_at')),''),
               r.source_published_at
             )
             ELSE r.source_published_at
           END AS source_published_at,
           r.local_received_at,
           COALESCE(sr.title,r.title) AS title,
           COALESCE(sr.summary,r.summary) AS summary,
           CASE
             WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
             THEN COALESCE(
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.link')),''),
               NULLIF(TRIM(json_extract(sr.raw_json,'$.item.url')),''),
               r.canonical_url
             )
             ELSE r.canonical_url
           END AS canonical_url,
           COALESCE(sr.content_sha256,r.content_sha256) AS content_sha256,
           COALESCE(sr.raw_json,r.raw_json) AS raw_json,
           CASE WHEN sr.revision_kind='delete'
                THEN 'deleted' ELSE r.observation_status END AS observation_status,
           COALESCE(sr.revision_no,0) AS latest_revision_no,
           COALESCE(sr.revision_kind,'new') AS latest_revision_kind,
           COALESCE(sr.revision_at,r.local_received_at) AS latest_revision_at
    FROM page_evidence_observations page_observation
    CROSS JOIN raw_observations r
    LEFT JOIN ranked_source_revisions sr
      ON sr.observation_id=r.observation_id
     AND sr.source_revision_rank=1
    WHERE r.observation_id=page_observation.observation_id
),
page_event_sources AS MATERIALIZED (
    SELECT source_link.event_id,
           raw.observation_id,raw.source_published_at,raw.local_received_at,
           page.event_date,
           source_catalog.name AS source_name,
           {source_title_sql} AS title,
           {source_summary_sql} AS summary,
           CASE WHEN revision.revision_kind='delete'
                THEN 'deleted' ELSE raw.observation_status END AS observation_status,
           source_link.relation_type
    FROM paged_canonical page
    CROSS JOIN event_observations source_link
    CROSS JOIN raw_observations raw
    JOIN sources source_catalog ON source_catalog.source_id=raw.source_id
    LEFT JOIN source_revisions revision
      ON revision.observation_id=raw.observation_id
     AND revision.revision_no=(
           SELECT MAX(latest_revision.revision_no)
           FROM source_revisions latest_revision
           WHERE latest_revision.observation_id=raw.observation_id
         )
    WHERE source_link.event_id=page.event_id
      AND raw.observation_id=source_link.observation_id
),
ranked_event_sources AS (
    SELECT source.event_id,
           source.observation_id,source.source_published_at,source.local_received_at,
           source.source_name,source.title,source.summary,source.observation_status,
           ROW_NUMBER() OVER (
               PARTITION BY source.event_id
               ORDER BY CASE
                          WHEN DATE(source.source_published_at)=DATE(source.event_date)
                           AND LENGTH(TRIM(COALESCE(source.summary,'')))>=40
                          THEN 0 ELSE 1
                        END,
                        source.local_received_at DESC,source.observation_id DESC
           ) AS source_rank
    FROM page_event_sources source
    WHERE source.relation_type!='filtered_aggregated_noise'
      AND source.observation_status!='deleted'
),
event_source_rollup AS (
    SELECT source.event_id,
           SUM(CASE WHEN source.observation_status!='deleted' THEN 1 ELSE 0 END)
             AS captured_source_count
    FROM page_event_sources source
    GROUP BY source.event_id
),
sec_current_supported_fact_slots AS (
    SELECT current_version.event_id,current_version.version
    FROM paged_canonical slot_event
    JOIN event_versions current_version
      ON current_version.event_id=slot_event.event_id
     AND current_version.version=slot_event.current_version
    JOIN event_evidence slot_evidence
      ON slot_evidence.event_id=current_version.event_id
     AND slot_evidence.evidence_id=json_extract(
           current_version.facts_json,'$.evidence_id'
         )
    JOIN json_each(
      CASE WHEN json_valid(current_version.facts_json)
           THEN current_version.facts_json ELSE '{{}}' END,
      '$.claim_fact_slots.facts'
    ) slot
    WHERE LENGTH(TRIM(COALESCE(json_extract(slot.value,'$.predicate'),'')))>0
      AND CAST(json_extract(slot.value,'$.event_type_compatible') AS INTEGER)=1
      AND LENGTH(TRIM(COALESCE(json_extract(slot.value,'$.action_text'),'')))>0
      AND LENGTH(TRIM(COALESCE(json_extract(slot.value,'$.object'),'')))>0
      AND LENGTH(TRIM(COALESCE(json_extract(slot.value,'$.evidence_sentence'),'')))>=20
      AND LOWER(json_extract(
            current_version.facts_json,'$.claim_fact_slots.event_type'
          ))=LOWER(TRIM(slot_event.event_type))
      AND INSTR(slot_evidence.evidence_passage,json_extract(
            slot.value,'$.evidence_sentence'
          ))>0
      AND INSTR(json_extract(
            current_version.facts_json,'$.public_fact_summary'
          ),json_extract(slot.value,'$.action_text'))>0
      AND INSTR(json_extract(
            current_version.facts_json,'$.public_fact_summary'
          ),json_extract(slot.value,'$.object'))>0
      AND (
        json_extract(slot.value,'$.subject_binding') IN (
          'EXPLICIT_ISSUER','EXPLICIT_ISSUER_CONTEXT'
        )
        AND CAST(json_extract(
              slot.value,'$.issuer_name_explicit_in_passage'
            ) AS INTEGER)=1
        AND LOWER(TRIM(json_extract(slot.value,'$.subject')))=LOWER(TRIM(
              json_extract(current_version.facts_json,'$.claim_subject')
            ))
      )
    GROUP BY current_version.event_id,current_version.version
),
ranked_rough_reviews AS (
    SELECT job.job_id,job.event_id,job.payload_json,job.updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY job.event_id
               ORDER BY job.updated_at DESC,job.job_id DESC
           ) AS rough_rank
    FROM paged_canonical page
    CROSS JOIN pipeline_jobs job
    WHERE job.event_id=page.event_id
      AND job.status='COMPLETED_AUTHORIZED_ROUGH_REVIEW'
),
ranked_light_followups AS (
    SELECT job.job_id,job.event_id,job.status,job.payload_json,job.updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY job.event_id
               ORDER BY job.updated_at DESC,job.job_id DESC
           ) AS followup_rank
    FROM paged_canonical page
    CROSS JOIN pipeline_jobs job
    WHERE job.event_id=page.event_id
      AND job.job_type='light_verification_followup'
      AND job.status IN ('PENDING_EVIDENCE_REVIEW','PENDING_HUMAN_REVIEW')
),
event_reader_evidence AS (
    SELECT ev.event_id,
           COUNT(*) AS citable_evidence_count
    FROM paged_canonical ce
    JOIN event_evidence ev ON ev.event_id=ce.event_id
    JOIN event_evidence_relations rel
      ON rel.event_id=ev.event_id
     AND rel.evidence_id=ev.evidence_id
     AND rel.event_version=ce.current_version
    JOIN current_source_content ro ON ro.observation_id=ev.observation_id
    JOIN sources src ON src.source_id=ro.source_id
    JOIN event_versions current_version
      ON current_version.event_id=ce.event_id
     AND current_version.version=ce.current_version
    LEFT JOIN sec_current_supported_fact_slots sec_slot
      ON sec_slot.event_id=ce.event_id
     AND sec_slot.version=ce.current_version
    WHERE TRIM(COALESCE(ev.evidence_url,''))!=''
      AND LENGTH(TRIM(COALESCE(ev.evidence_passage,'')))>=40
      AND (
        (
          ev.evidence_status IN (
            'machine_extracted_unreviewed','candidate_passage',
            'confirmed_primary','accepted_manual_primary_evidence',
            'accepted_light_primary_evidence'
          )
          AND rel.relation_status IN ('SCOPED_MATCH','HUMAN_CONFIRMED')
          AND {_SEC_CURRENT_FACT_SLOT_MATCH_SQL}
          AND rel.contract_version='event-admission-v3'
          AND rel.evidence_fingerprint=json_extract(
                current_version.facts_json,'$.evidence_fingerprint'
              )
        ) OR (
          ev.evidence_status='accepted_dual_human_primary_evidence'
          AND rel.relation_status='HUMAN_CONFIRMED'
          AND {_DUAL_HUMAN_RECEIPT_MATCH_SQL}
        )
      )
      AND rel.subject_match=1
      AND rel.event_claim_supported=1
      AND rel.date_coherent=1
      AND {_CURRENT_EVIDENCE_PASSAGE_MATCH_SQL}
      AND (
        UPPER(src.authority_tier) IN ('P0','P1')
        OR UPPER(src.authority_tier) GLOB 'P0_*'
        OR UPPER(src.authority_tier) GLOB 'P1_*'
      )
    GROUP BY ev.event_id
),
event_public AS (
    SELECT canonical.*,
           light.status AS light_followup_status,
           light.updated_at AS light_followup_updated_at,
           CASE WHEN json_valid(light.payload_json)
             THEN json_extract(light.payload_json,'$.light_verification_followup.expected_next_action')
           END AS light_followup_next_action,
           CASE
             WHEN canonical.status='rejected' THEN 'excluded'
             WHEN light.job_id IS NOT NULL AND canonical.status!='weak' THEN 'pending_verification'
             WHEN canonical.status='verified' THEN 'verified'
             WHEN canonical.status='weak' OR (
               rough.job_id IS NOT NULL
               AND CASE WHEN json_valid(rough.payload_json)
                 THEN COALESCE(
                   json_extract(rough.payload_json,'$.rough_review.outcome'),
                   CASE WHEN UPPER(COALESCE(
                     json_extract(rough.payload_json,'$.rough_review.decision_status'),''
                   ))='INSUFFICIENT' THEN 'ROUGH_INSUFFICIENT' END
                 )
               END='ROUGH_INSUFFICIENT'
             ) THEN 'insufficient'
             WHEN canonical.status='candidate' AND rough.job_id IS NOT NULL THEN 'rough_reviewed'
             ELSE 'pending_verification'
           END AS public_state,
           COALESCE(
             CASE WHEN json_valid(rough.payload_json)
               THEN json_extract(rough.payload_json,'$.rough_review.reviewed_at')
             END,
              rough.updated_at
           ) AS reviewed_at,
           COALESCE(reader_evidence.citable_evidence_count,0) AS citable_evidence_count,
           CASE WHEN json_valid(current_version.facts_json)
             THEN NULLIF(TRIM(json_extract(
               current_version.facts_json,'$.public_fact_summary'
             )),'')
           END AS public_fact_summary,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.claim_subject')
            END AS claim_subject,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.claim_action')
            END AS claim_action,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.claim_stage')
            END AS claim_stage,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.known_at')
            END AS known_at,
           CASE WHEN COALESCE(
             NULLIF(TRIM(canonical.company_name),''),
             NULLIF(TRIM(canonical.ticker_at_event),''),
             ''
           )!=''
             THEN 1 ELSE 0
           END AS reader_has_subject,
           CASE WHEN json_valid(current_version.facts_json) AND LENGTH(COALESCE(
             NULLIF(TRIM(json_extract(current_version.facts_json,'$.public_fact_summary')),''),
             ''
            ))>=20
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_subject'),''
              )))>=2
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_action'),''
              )))>=3
              AND UPPER(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_stage'),''
              ))) IN ('PROPOSED','FILED','DISCLOSED','EFFECTIVE','ONGOING','COMPLETED')
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.known_at'),''
              )))>=20
              THEN 1 ELSE 0 END AS reader_has_fact_summary,
           CASE WHEN
             COALESCE(
               NULLIF(TRIM(canonical.company_name),''),
               NULLIF(TRIM(canonical.ticker_at_event),''),
               ''
             )!=''
             AND COALESCE(reader_evidence.citable_evidence_count,0)>0
             AND json_valid(current_version.facts_json)
              AND LENGTH(COALESCE(
               NULLIF(TRIM(json_extract(current_version.facts_json,'$.public_fact_summary')),''),
               ''
              ))>=20
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_subject'),''
              )))>=2
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_action'),''
              )))>=3
              AND UPPER(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_stage'),''
              ))) IN ('PROPOSED','FILED','DISCLOSED','EFFECTIVE','ONGOING','COMPLETED')
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.known_at'),''
              )))>=20
              THEN 1 ELSE 0
           END AS reader_ready
    FROM paged_canonical canonical
    LEFT JOIN ranked_rough_reviews rough
      ON rough.event_id=canonical.event_id AND rough.rough_rank=1
    LEFT JOIN ranked_light_followups light
      ON light.event_id=canonical.event_id AND light.followup_rank=1
    LEFT JOIN event_reader_evidence reader_evidence
      ON reader_evidence.event_id=canonical.event_id
    LEFT JOIN event_versions current_version
      ON current_version.event_id=canonical.event_id
     AND current_version.version=canonical.current_version
)
""".strip()


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _json(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else value


class LedgerRepository:
    """Read-only product query adapter over the existing Schema 12 ledger."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(f"ledger database not found: {self.path}")
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        register_sqlite_integrity_functions(connection)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def schema_version(self) -> int:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM event_ledger_schema"
            ).fetchone()
            return int(row["version"] or 0)

    def health(self, *, run_integrity_check: bool = True) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            quick_check = (
                connection.execute("PRAGMA quick_check").fetchone()[0]
                if run_integrity_check
                else "deferred"
            )
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "sources",
                    "raw_observations",
                    "canonical_events",
                    "event_versions",
                    "event_evidence",
                    "event_market_metrics",
                    "pipeline_jobs",
                    "alert_outbox",
                )
            }
            counts["public_visible_events"] = int(
                connection.execute(
                    """SELECT COUNT(*)
                       FROM canonical_events e
                       WHERE NOT EXISTS (
                           SELECT 1 FROM event_versions vnoise
                           WHERE vnoise.event_id=e.event_id
                             AND vnoise.version=e.current_version
                             AND vnoise.change_reason='official_nonfinancial_notice'
                       )"""
                ).fetchone()[0]
            )
            event_status = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM canonical_events GROUP BY status"
                )
            }
            audit = {
                "trading_boundary_violations": connection.execute(
                    "SELECT COUNT(*) FROM canonical_events WHERE no_trading != 1"
                ).fetchone()[0],
                "auto_verification_violations": connection.execute(
                    "SELECT COUNT(*) FROM event_evidence WHERE auto_verification_allowed != 0"
                ).fetchone()[0],
                "market_feature_leakage_violations": connection.execute(
                    "SELECT COUNT(*) FROM event_market_metrics WHERE allowed_as_model_feature != 0"
                ).fetchone()[0],
            }
            last_event_update = connection.execute(
                "SELECT MAX(last_updated_at) FROM canonical_events"
            ).fetchone()[0]
            last_new_event_at = connection.execute(
                "SELECT MAX(first_seen_at) FROM canonical_events"
            ).fetchone()[0]
        return {
            "status": "ok"
            if (quick_check == "ok" or not run_integrity_check) and not any(audit.values())
            else "degraded",
            "database": str(self.path),
            "database_bytes": self.path.stat().st_size,
            "schema_version": self.schema_version(),
            "quick_check": quick_check,
            "integrity_check_source": "live_database" if run_integrity_check else "not_run",
            "last_event_update": last_event_update,
            "last_new_event_at": last_new_event_at,
            "counts": counts,
            "event_status": event_status,
            "audit": audit,
        }

    def capture_source_generation(self) -> dict[str, Any]:
        """Return a cheap watermark for receipt-bound capture interpretation.

        The interpretation worker uses this only after it has exhausted the
        historical backlog.  An unchanged watermark means no observation was
        added or revised, so rebuilding the much heavier recovery plan would
        be wasted work.
        """

        with closing(self.connect()) as connection:
            observations = connection.execute(
                "SELECT COUNT(*) AS n,MAX(local_received_at) AS latest FROM raw_observations"
            ).fetchone()
            revisions = connection.execute(
                "SELECT COUNT(*) AS n,MAX(revision_at) AS latest FROM source_revisions"
            ).fetchone()
            relations = connection.execute(
                "SELECT COUNT(*) AS n,MAX(linked_at) AS latest FROM event_observations"
            ).fetchone()
            evidence = connection.execute(
                "SELECT COUNT(*) AS n,MAX(updated_at) AS latest FROM event_evidence"
            ).fetchone()
        return {
            "observation_count": int(observations["n"] or 0),
            "latest_observation_at": observations["latest"],
            "revision_count": int(revisions["n"] or 0),
            "latest_revision_at": revisions["latest"],
            "relation_count": int(relations["n"] or 0),
            "latest_relation_at": relations["latest"],
            "evidence_count": int(evidence["n"] or 0),
            "latest_evidence_at": evidence["latest"],
        }

    def capture_interpretation_candidate_count(self) -> int:
        """Count current receipt-bound DeepSeek candidates without materializing them."""

        with closing(self.connect()) as connection:
            row = connection.execute(
                f"""WITH {_CAPTURE_INTERPRETATION_CANDIDATE_CTES}
                    SELECT COUNT(*) FROM eligible_interpretation_capture"""
            ).fetchone()
        return int(row[0] or 0)

    def capture_interpretation_candidates(
        self,
        *,
        limit: int = 250,
        offset: int = 0,
        order: str = "fair",
        after: tuple[int, str, str] | None = None,
    ) -> list[dict[str, str]]:
        """Load one bounded window of receipt-bound LLM candidates.

        This is the scheduler path, not the historical recovery-report path.
        It intentionally excludes orphan captures and official/refetch buckets,
        preserves the exact immutable receipt used by the single-job runner,
        and never loads more than 1,000 capture payloads into memory.  The
        ``recent`` lane keeps new/revised captures responsive.  The ``fair``
        lane supports a durable keyset cursor, so a continuously changing head
        of the ledger cannot reset or starve the historical sweep.
        """

        limit = max(1, min(int(limit), 1_000))
        offset = max(0, int(offset))
        if order not in {"fair", "recent"}:
            raise ValueError(f"unsupported capture interpretation order: {order}")
        bucket_priority = "CASE bucket WHEN 'NO_URL_RAW_ONLY' THEN 0 ELSE 1 END"
        where = ""
        params: list[Any] = []
        if order == "fair" and after is not None:
            after_priority, after_event_id, after_observation_id = after
            where = f"""WHERE ({bucket_priority}>?
                           OR ({bucket_priority}=? AND event_id>?)
                           OR ({bucket_priority}=? AND event_id=? AND observation_id>?))"""
            params.extend(
                (
                    int(after_priority),
                    int(after_priority),
                    str(after_event_id),
                    int(after_priority),
                    str(after_event_id),
                    str(after_observation_id),
                )
            )
        ordering = (
            f"{bucket_priority},event_id,observation_id"
            if order == "fair"
            else "COALESCE(latest_revision_at,local_received_at) DESC,event_id,observation_id"
        )
        # OFFSET remains only for backwards-compatible bounded diagnostics.  The
        # production fair scheduler passes ``after`` and therefore never uses it.
        params.extend((limit, 0 if after is not None else offset))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""WITH {_CAPTURE_INTERPRETATION_CANDIDATE_CTES}
                    SELECT bucket,current_version,event_id,observation_id,source_id,external_id,
                           source_published_at,local_received_at,canonical_url,
                           content_sha256,raw_json,latest_revision_no,
                           latest_revision_kind,latest_revision_at
                    FROM eligible_interpretation_capture
                    {where}
                    ORDER BY {ordering}
                    LIMIT ? OFFSET ?""",
                params,
            ).fetchall()

        candidates: list[dict[str, str]] = []
        for row in rows:
            item = dict(row)
            raw_payload_sha256 = hashlib.sha256(
                str(item.pop("raw_json", "") or "").encode("utf-8")
            ).hexdigest()
            receipt_payload = {
                "source_id": item.get("source_id"),
                "external_id": item.get("external_id"),
                "semantic_content_sha256": item.get("content_sha256"),
                "canonical_url": item.get("canonical_url"),
                "source_published_at": item.get("source_published_at"),
                "local_received_at": item.get("local_received_at"),
                "latest_revision_no": item.get("latest_revision_no"),
                "latest_revision_kind": item.get("latest_revision_kind"),
                "raw_payload_sha256": raw_payload_sha256,
            }
            candidates.append(
                {
                    "event_id": str(item["event_id"]),
                    "event_version": int(item.get("current_version") or 0),
                    "observation_id": str(item["observation_id"]),
                    "capture_receipt_sha256": hashlib.sha256(
                        json.dumps(
                            receipt_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "bucket": str(item["bucket"]),
                    "bucket_priority": (
                        0 if str(item["bucket"]) == "NO_URL_RAW_ONLY" else 1
                    ),
                    "latest_revision_at": str(
                        item.get("latest_revision_at")
                        or item.get("local_received_at")
                        or ""
                    ),
                }
            )
        return candidates

    def capture_interpretation_eligibility(
        self,
        event_id: str,
        *,
        observation_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the single fail-closed gate for external capture explanation.

        External AI is intentionally narrower than public event visibility. An
        event remains browsable regardless of this result, but an explanation
        may be generated or displayed only while the canonical event has no
        evidence rows at all and a current non-deleted raw/P2 capture contains
        text.  A current official URL wins over AI and is routed to refetch.
        """

        with closing(self.connect()) as connection:
            event_row = connection.execute(
                "SELECT event_id,current_version FROM canonical_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            evidence_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM event_evidence WHERE event_id=?",
                    (event_id,),
                ).fetchone()[0]
            )
        if event_row is None:
            return {
                "display": False,
                "eligible": False,
                "reason_code": "EVENT_NOT_FOUND",
                "current_event_version": None,
                "evidence_count": evidence_count,
                "eligible_observation_ids": [],
            }

        event_version = int(event_row["current_version"] or 0)
        if evidence_count > 0:
            return {
                "current_event_version": event_version,
                "evidence_count": evidence_count,
                "capture_count": None,
                "eligible_observation_ids": [],
                "display": False,
                "eligible": False,
                "reason_code": "EVIDENCE_PRESENT",
            }

        captures = [
            item
            for item in self.captured_sources(event_id)
            if item.get("observation_status") != "deleted"
        ]
        selected = (
            next(
                (
                    item
                    for item in captures
                    if str(item.get("observation_id") or "") == observation_id
                ),
                None,
            )
            if observation_id
            else None
        )
        base = {
            "current_event_version": event_version,
            "evidence_count": evidence_count,
            "capture_count": len(captures),
            "eligible_observation_ids": [],
        }
        if observation_id and selected is None:
            return {
                **base,
                "display": False,
                "eligible": False,
                "reason_code": "CAPTURE_NOT_FOUND",
            }
        official_refetch = any(
            str(item.get("canonical_url") or "").strip()
            and (
                str(item.get("authority_tier") or "").upper() in {"P0", "P1"}
                or str(item.get("authority_tier") or "").upper().startswith(("P0_", "P1_"))
            )
            for item in captures
        )
        if official_refetch:
            return {
                **base,
                "display": False,
                "eligible": False,
                "reason_code": "REFETCH_PRIMARY_SOURCE",
            }

        text_captures = [
            item
            for item in captures
            if str(item.get("title") or "").strip()
            or str(item.get("summary") or "").strip()
        ]
        if not text_captures or (observation_id and selected not in text_captures):
            return {
                **base,
                "display": False,
                "eligible": False,
                "reason_code": "NO_CAPTURE_TEXT",
            }
        eligible_ids = [
            str(item.get("observation_id") or "")
            for item in text_captures
            if str(item.get("observation_id") or "")
        ]
        bucket = (
            "P2_CAPTURE_ONLY"
            if any(str(item.get("canonical_url") or "").strip() for item in captures)
            else "NO_URL_RAW_ONLY"
        )
        return {
            **base,
            "display": True,
            "eligible": True,
            "reason_code": "NO_EVENT_EVIDENCE",
            "bucket": bucket,
            "eligible_observation_ids": eligible_ids,
        }

    def capture_interpretation_context(
        self,
        event_id: str,
        observation_id: str,
    ) -> dict[str, Any] | None:
        """Load only the fields admitted to the receipt-bound LLM contract.

        This deliberately bypasses the public-reader CTE.  A retained capture
        is discovery data, not a public conclusion, and interpreting it should
        not require recomputing reader eligibility for the entire ledger.
        """

        with closing(self.connect()) as connection:
            event_row = connection.execute(
                "SELECT * FROM canonical_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if event_row is None:
                return None
            capture_row = connection.execute(
                f"""WITH {_CURRENT_SOURCE_CONTENT_CTES}
                    SELECT eo.relation_type,eo.linked_at,
                           r.observation_id,r.source_id,r.external_id,
                           r.source_published_at,r.local_received_at,
                           r.title,r.summary,r.canonical_url,r.content_sha256,
                           r.raw_json,
                           r.observation_status,r.latest_revision_no,
                           r.latest_revision_kind,r.latest_revision_at,
                           s.name AS source_name,s.source_type,s.authority_tier
                    FROM event_observations eo
                    JOIN current_source_content r
                      ON r.observation_id=eo.observation_id
                    JOIN sources s ON s.source_id=r.source_id
                    WHERE eo.event_id=? AND r.observation_id=?
                    LIMIT 1""",
                (event_id, observation_id),
            ).fetchone()
        if capture_row is None:
            return None
        capture = dict(capture_row)
        raw_payload = str(capture.pop("raw_json", "") or "")
        raw_payload_sha256 = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
        receipt_payload = {
            "source_id": capture.get("source_id"),
            "external_id": capture.get("external_id"),
            "semantic_content_sha256": capture.get("content_sha256"),
            "canonical_url": capture.get("canonical_url"),
            "source_published_at": capture.get("source_published_at"),
            "local_received_at": capture.get("local_received_at"),
            "latest_revision_no": capture.get("latest_revision_no"),
            "latest_revision_kind": capture.get("latest_revision_kind"),
            "raw_payload_sha256": raw_payload_sha256,
        }
        capture["raw_payload_sha256"] = raw_payload_sha256
        capture["capture_receipt_sha256"] = hashlib.sha256(
            json.dumps(
                receipt_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        capture["semantic_content_sha256"] = capture.pop("content_sha256", None)
        return {"event": dict(event_row), "capture": capture}

    def shadow_batch(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        order: str = "latest",
        event_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Load recent shadow-router inputs with two bounded SQL queries.

        The old implementation called the full public-reader CTE once for the
        page and again for every event.  On the production ledger that made a
        bounded shadow batch take longer than the whole worker deadline.

        ``order='event_id'`` plus ``offset`` is reserved for the durable fair
        queue.  It walks the whole canonical ledger in stable windows while the
        default recent lane keeps newly changed events responsive.
        """

        requested_event_ids = list(
            dict.fromkeys(
                str(value).strip()
                for value in (event_ids or [])
                if str(value).strip()
            )
        )[:200]
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        sort_orders = {
            "latest": "ce.last_updated_at DESC,ce.event_id DESC",
            "event_id": "ce.event_id ASC",
        }
        if order not in sort_orders:
            raise ValueError(f"unsupported shadow batch order: {order}")
        with closing(self.connect()) as connection:
            # Select the bounded event window before touching observations or
            # revisions.  The previous query embedded the global
            # ``_CURRENT_SOURCE_CONTENT_CTES`` and ranked every retained source
            # revision before SQLite could apply LIMIT.  On the production
            # ledger that made a 200-event shadow window consume the outer
            # ten-minute worker deadline.
            event_filter = ""
            event_params: list[Any] = []
            if requested_event_ids:
                event_filter = "WHERE ce.event_id IN (" + ",".join(
                    "?" for _ in requested_event_ids
                ) + ")"
                event_params.extend(requested_event_ids)
            event_rows = connection.execute(
                f"""SELECT ce.*,v.facts_json
                    FROM canonical_events ce
                    JOIN event_versions v
                      ON v.event_id=ce.event_id AND v.version=ce.current_version
                    {event_filter}
                    ORDER BY {sort_orders[order]}
                    LIMIT ? OFFSET ?""",
                (*event_params, limit, offset),
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in event_rows]
            preferred_source_by_event: dict[str, dict[str, Any]] = {
                event_id: {} for event_id in event_ids
            }
            evidence_by_event: dict[str, list[dict[str, Any]]] = {
                event_id: [] for event_id in event_ids
            }
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                source_rows = connection.execute(
                    f"""WITH ranked_source AS (
                          SELECT eo.event_id,r.observation_id,r.title,r.summary,r.source_id,
                                 r.external_id,r.canonical_url,r.content_sha256,r.raw_json,
                                 r.latest_revision_no,r.latest_revision_kind,r.latest_revision_at,
                                 r.source_published_at,r.local_received_at,
                                 ROW_NUMBER() OVER (
                                   PARTITION BY eo.event_id
                                   ORDER BY r.local_received_at DESC,
                                            r.observation_id DESC
                                 ) AS source_rank
                          FROM event_observations eo
                          JOIN latest_source_content r
                            ON r.observation_id=eo.observation_id
                          WHERE eo.event_id IN ({placeholders})
                            AND eo.relation_type!='filtered_aggregated_noise'
                            AND r.observation_status!='deleted'
                        )
                        SELECT event_id,observation_id,title,summary,source_id,external_id,
                               canonical_url,content_sha256,raw_json,latest_revision_no,
                               latest_revision_kind,latest_revision_at,
                               source_published_at,local_received_at
                        FROM ranked_source
                        WHERE source_rank=1""",
                    event_ids,
                ).fetchall()
                for row in source_rows:
                    item = dict(row)
                    event_id = str(item.pop("event_id"))
                    raw_payload = str(item.pop("raw_json", "") or "")
                    item["raw_payload_sha256"] = hashlib.sha256(
                        raw_payload.encode("utf-8")
                    ).hexdigest()
                    preferred_source_by_event[event_id] = item
                evidence_rows = connection.execute(
                    f"""WITH ranked_evidence AS (
                          SELECT ev.*,r.source_id,src.authority_tier,
                                 rel.event_version AS relation_event_version,
                                 rel.relation_status,rel.subject_match,
                                 rel.event_claim_supported,rel.date_coherent,
                                 rel.modality,rel.evidence_fingerprint,
                                 ROW_NUMBER() OVER (
                                   PARTITION BY ev.event_id
                                   ORDER BY ev.passage_score DESC,
                                            ev.updated_at DESC,ev.evidence_id
                                 ) AS evidence_rank
                          FROM event_evidence ev
                          JOIN latest_source_content r
                            ON r.observation_id=ev.observation_id
                          JOIN sources src ON src.source_id=r.source_id
                          JOIN canonical_events current_event
                            ON current_event.event_id=ev.event_id
                          JOIN event_evidence_relations rel
                            ON rel.event_id=ev.event_id
                           AND rel.evidence_id=ev.evidence_id
                           AND rel.event_version=current_event.current_version
                          WHERE ev.event_id IN ({placeholders})
                            AND r.observation_status!='deleted'
                            AND rel.relation_status IN ('SCOPED_MATCH','HUMAN_CONFIRMED')
                            AND rel.subject_match=1
                            AND rel.event_claim_supported=1
                            AND rel.date_coherent=1
                        )
                        SELECT * FROM ranked_evidence
                        WHERE evidence_rank<=5
                        ORDER BY event_id,evidence_rank""",
                    event_ids,
                ).fetchall()
                for row in evidence_rows:
                    item = dict(row)
                    item.pop("evidence_rank", None)
                    evidence_by_event[str(item["event_id"])].append(item)

        result: list[dict[str, Any]] = []
        for row in event_rows:
            item = dict(row)
            facts = _json(item.pop("facts_json"), {})
            event_id = str(item["event_id"])
            preferred_source = preferred_source_by_event[event_id]
            result.append(
                {
                    "detail": {
                        "event": item,
                        "current_version": {"facts": facts},
                        "preferred_source": preferred_source,
                    },
                    "evidence": evidence_by_event[event_id],
                }
            )
        return result

    def overview(self, recent_limit: int = 12, *, run_integrity_check: bool = True) -> dict[str, Any]:
        health = self.health(run_integrity_check=run_integrity_check)
        with closing(self.connect()) as connection:
            job_status = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM pipeline_jobs GROUP BY status"
                )
            }
            review_counts = connection.execute(
                PUBLIC_EVENT_STATE_CTE
                + """
                   SELECT COUNT(DISTINCT j.event_id) AS review_queue,
                          COUNT(DISTINCT CASE WHEN e.reader_ready=1 THEN j.event_id END)
                            AS reader_review_queue
                   FROM pipeline_jobs j
                   JOIN event_public e ON e.event_id=j.event_id
                   WHERE e.status IN ('candidate','weak')
                     AND j.status IN (
                         'PENDING_PRIMARY_EVIDENCE',
                         'PENDING_EVIDENCE_REVIEW',
                         'PENDING_HUMAN_REVIEW'
                     )"""
            ).fetchone()
            review_queue = int(review_counts["review_queue"] or 0)
            reader_review_queue = int(review_counts["reader_review_queue"] or 0)
            alert_status = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM alert_outbox GROUP BY status"
                )
            }
            public_funnel = self._public_funnel(connection)
            reader_quality = self._reader_quality(connection)
        return {
            **health,
            "public_funnel": public_funnel,
            "reader_funnel": reader_quality["reader_funnel"],
            "reader_quality": reader_quality,
            "review_queue": review_queue,
            "reader_review_queue": reader_review_queue,
            "non_citation_ready_inventory": int(
                reader_quality["non_citation_ready_inventory"]
            ),
            # Deprecated numeric aliases retained for older clients.  These
            # records remain browseable in Public; the value only measures
            # which current event versions cannot yet support a formal claim.
            "reader_hidden_inventory": int(
                reader_quality["non_citation_ready_inventory"]
            ),
            "discovery_backlog": int(
                reader_quality["non_citation_ready_inventory"]
            ),
            "inventory_contract": {
                "non_citation_ready_inventory": {
                    "authoritative": True,
                    "definition": (
                        "all canonical events whose current version is not citation-ready; "
                        "they remain browseable in the public event feed"
                    ),
                },
                "reader_hidden_inventory": {
                    "deprecated": True,
                    "replacement": "non_citation_ready_inventory",
                    "definition": (
                        "legacy numeric alias; these events are not hidden from Public"
                    ),
                },
                "discovery_backlog": {
                    "deprecated": True,
                    "replacement": "non_citation_ready_inventory",
                    "definition": (
                        "legacy numeric alias; it includes every non-citation-ready canonical event, "
                        "not only discovery-stage leads"
                    ),
                },
            },
            "review_queue_non_citation_ready": max(
                0, review_queue - reader_review_queue
            ),
            # Deprecated alias: citation readiness is not a Public visibility
            # gate, so the historical name no longer describes the value.
            "review_queue_hidden_by_reader_gate": max(
                0, review_queue - reader_review_queue
            ),
            "rough_reviewed": int(job_status.get("COMPLETED_AUTHORIZED_ROUGH_REVIEW", 0)),
            "job_status": job_status,
            "alert_status": alert_status,
            "recent_events": self.list_events(
                status="verified",
                reader_ready=True,
                limit=recent_limit,
            )["items"],
            "source_health": self.list_source_health(),
        }

    def product_metrics(
        self,
        *,
        now: datetime | None = None,
        window_days: int = 30,
    ) -> dict[str, Any]:
        """Measure user-value signals without substituting engineering health.

        Every metric declares its sample and source. Metrics that need a human
        sampling programme remain explicitly unavailable instead of receiving a
        proxy score from model output or test counts.
        """
        measured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        window_days = min(365, max(1, int(window_days)))
        cutoff = measured_at.timestamp() - window_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        now_iso = measured_at.isoformat()

        def percentile(values: list[float], fraction: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
            return ordered[index]

        with closing(self.connect()) as connection:
            latency_rows = connection.execute(
                """SELECT (julianday(local_received_at)-julianday(source_published_at))*86400.0 AS seconds
                   FROM raw_observations
                   WHERE source_published_at IS NOT NULL
                     AND TRIM(source_published_at)!=''
                     AND local_received_at>=?
                     AND julianday(local_received_at)>=julianday(source_published_at)""",
                (cutoff_iso,),
            ).fetchall()
            latencies = [float(row["seconds"]) for row in latency_rows if row["seconds"] is not None]
            linked_observations = int(
                connection.execute("SELECT COUNT(*) FROM event_observations").fetchone()[0]
            )
            linked_events = int(
                connection.execute("SELECT COUNT(DISTINCT event_id) FROM event_observations").fetchone()[0]
            )
            total_events = int(connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0])
            cited_events = int(
                connection.execute(
                    """SELECT COUNT(*) FROM canonical_events e
                       WHERE EXISTS (
                         SELECT 1 FROM event_evidence ev
                         WHERE ev.event_id=e.event_id
                           AND ev.evidence_url IS NOT NULL AND TRIM(ev.evidence_url)!=''
                           AND ev.evidence_passage IS NOT NULL AND TRIM(ev.evidence_passage)!=''
                       )"""
                ).fetchone()[0]
            )
            closed_events = int(
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_events WHERE status IN ('verified','rejected')"
                ).fetchone()[0]
            )
            conflict_events = int(
                connection.execute(
                    """SELECT COUNT(DISTINCT event_id) FROM event_evidence
                       WHERE LOWER(evidence_status) LIKE '%conflict%'
                          OR LOWER(evidence_status) LIKE '%disput%'"""
                ).fetchone()[0]
            )
            queue_rows = connection.execute(
                """SELECT (julianday(?)-julianday(MIN(j.created_at)))*86400.0 AS seconds
                   FROM pipeline_jobs j
                   JOIN canonical_events e ON e.event_id=j.event_id
                   WHERE e.status IN ('candidate','weak')
                     AND j.status IN (
                       'PENDING_PRIMARY_EVIDENCE',
                       'PENDING_EVIDENCE_REVIEW',
                       'PENDING_HUMAN_REVIEW'
                     )
                   GROUP BY j.event_id""",
                (now_iso,),
            ).fetchall()
            queue_ages = [max(0.0, float(row["seconds"])) for row in queue_rows if row["seconds"] is not None]
            trust_violations = int(
                connection.execute(
                    """SELECT
                         (SELECT COUNT(*) FROM canonical_events WHERE no_trading!=1) +
                         (SELECT COUNT(*) FROM event_evidence WHERE auto_verification_allowed!=0) +
                         (SELECT COUNT(*) FROM event_market_metrics WHERE allowed_as_model_feature!=0)"""
                ).fetchone()[0]
            )

        def measured(
            metric_id: str,
            value: float,
            unit: str,
            sample_size: int,
            source: str,
        ) -> dict[str, Any]:
            return {
                "id": metric_id,
                "status": "MEASURED",
                "value": round(value, 2),
                "unit": unit,
                "sample_size": sample_size,
                "source": source,
            }

        def unavailable(metric_id: str, reason: str, source: str) -> dict[str, Any]:
            return {
                "id": metric_id,
                "status": "UNAVAILABLE",
                "value": None,
                "unit": None,
                "sample_size": 0,
                "source": source,
                "reason": reason,
            }

        metrics: list[dict[str, Any]] = []
        p50 = percentile(latencies, 0.50)
        p95 = percentile(latencies, 0.95)
        metrics.append(
            measured("capture_latency_p50", p50, "seconds", len(latencies), "raw_observations")
            if p50 is not None
            else unavailable("capture_latency_p50", "no comparable source and receipt timestamps", "raw_observations")
        )
        metrics.append(
            measured("capture_latency_p95", p95, "seconds", len(latencies), "raw_observations")
            if p95 is not None
            else unavailable("capture_latency_p95", "no comparable source and receipt timestamps", "raw_observations")
        )
        metrics.append(
            measured(
                "duplicate_compression_rate",
                100.0 * max(0, linked_observations - linked_events) / linked_observations,
                "percent",
                linked_observations,
                "event_observations",
            )
            if linked_observations
            else unavailable("duplicate_compression_rate", "no linked observations", "event_observations")
        )
        metrics.append(
            measured(
                "citable_evidence_coverage",
                100.0 * cited_events / total_events,
                "percent",
                total_events,
                "canonical_events+event_evidence",
            )
            if total_events
            else unavailable("citable_evidence_coverage", "no canonical events", "canonical_events+event_evidence")
        )
        metrics.append(
            measured(
                "evidence_closure_rate",
                100.0 * closed_events / total_events,
                "percent",
                total_events,
                "canonical_events",
            )
            if total_events
            else unavailable("evidence_closure_rate", "no canonical events", "canonical_events")
        )
        metrics.append(
            measured(
                "evidence_conflict_rate",
                100.0 * conflict_events / total_events,
                "percent",
                total_events,
                "canonical_events+event_evidence",
            )
            if total_events
            else unavailable("evidence_conflict_rate", "no canonical events", "canonical_events+event_evidence")
        )
        queue_p95 = percentile(queue_ages, 0.95)
        metrics.append(
            measured("review_queue_age_p95", queue_p95, "seconds", len(queue_ages), "pipeline_jobs")
            if queue_p95 is not None
            else unavailable("review_queue_age_p95", "no open review jobs", "pipeline_jobs")
        )
        metrics.append(measured("boundary_violations", float(trust_violations), "count", total_events, "ledger_constraints"))
        metrics.append(
            unavailable(
                "formal_conclusion_accuracy",
                "requires an independent human sample; model output and test counts are not substitutes",
                "human_quality_sample",
            )
        )
        metrics.append(
            unavailable(
                "reader_time_to_source",
                "client-side interaction telemetry is not enabled",
                "browser_interaction_measurement",
            )
        )
        return {
            "measured_at": measured_at.isoformat(),
            "window": {"days": window_days, "starts_at": cutoff_iso},
            "metrics": metrics,
            "engineering_health_is_not_product_quality": True,
        }

    @staticmethod
    def _rough_review_metadata(payload_json: Any, updated_at: Any) -> dict[str, str | None]:
        payload = _json(payload_json, {})
        rough_review = payload.get("rough_review") if isinstance(payload, dict) else None
        outcome = rough_review.get("outcome") if isinstance(rough_review, dict) else None
        if not outcome and isinstance(rough_review, dict):
            decision_status = str(rough_review.get("decision_status") or "").upper()
            if decision_status == "INSUFFICIENT":
                outcome = "ROUGH_INSUFFICIENT"
        reviewed_at = rough_review.get("reviewed_at") if isinstance(rough_review, dict) else None
        return {
            "outcome": str(outcome or "ROUGH_REVIEWED"),
            "reviewed_at": str(reviewed_at or updated_at) if reviewed_at or updated_at else None,
        }

    @staticmethod
    def _public_state(
        status: Any,
        rough_outcome: str | None,
        light_followup_status: str | None = None,
    ) -> str:
        normalized = str(status or "candidate").lower()
        if normalized == "rejected":
            return "excluded"
        if light_followup_status in {"PENDING_EVIDENCE_REVIEW", "PENDING_HUMAN_REVIEW"}:
            # A known weak record remains honestly insufficient.  Any other
            # active follow-up means a previous formal-looking disposition is
            # being reconciled and cannot be presented as settled verification.
            return "insufficient" if normalized == "weak" else "pending_verification"
        if normalized == "verified":
            return "verified"
        if rough_outcome == "ROUGH_INSUFFICIENT" or normalized == "weak":
            return "insufficient"
        if normalized == "candidate" and rough_outcome is not None:
            return "rough_reviewed"
        return "pending_verification"

    @staticmethod
    def _public_funnel(connection: sqlite3.Connection) -> dict[str, Any]:
        """Build one exhaustive public disposition for every canonical event.

        Formal canonical outcomes take precedence over rough-review metadata.
        A completed rough review is intentionally not presented as verification.
        """
        events = [
            dict(row)
            for row in connection.execute(
                "SELECT event_id,status FROM canonical_events ORDER BY event_id"
            )
        ]
        rough_by_event: dict[str, dict[str, str | None]] = {}
        for row in connection.execute(
            """SELECT event_id,payload_json,updated_at
               FROM pipeline_jobs
               WHERE status='COMPLETED_AUTHORIZED_ROUGH_REVIEW'
               ORDER BY updated_at DESC,job_id DESC"""
        ):
            event_id = str(row["event_id"])
            if event_id in rough_by_event:
                continue
            rough_by_event[event_id] = LedgerRepository._rough_review_metadata(
                row["payload_json"], row["updated_at"]
            )
        light_followup_by_event: dict[str, str] = {}
        for row in connection.execute(
            """SELECT event_id,status
               FROM (
                   SELECT event_id,status,
                          ROW_NUMBER() OVER (
                              PARTITION BY event_id ORDER BY updated_at DESC,job_id DESC
                          ) AS followup_rank
                   FROM pipeline_jobs
                   WHERE job_type='light_verification_followup'
                     AND status IN ('PENDING_EVIDENCE_REVIEW','PENDING_HUMAN_REVIEW')
               )
               WHERE followup_rank=1"""
        ):
            light_followup_by_event[str(row["event_id"])] = str(row["status"])

        buckets = {
            "verified": 0,
            "excluded": 0,
            "insufficient": 0,
            "pending_verification": 0,
            "rough_reviewed": 0,
        }
        insufficient_breakdown = {
            "rough_review": 0,
            "canonical_weak_without_rough_insufficient": 0,
        }
        for event in events:
            event_id = str(event["event_id"])
            status = str(event.get("status") or "candidate").lower()
            rough = rough_by_event.get(event_id)
            rough_outcome = str(rough["outcome"]) if rough else None
            public_state = LedgerRepository._public_state(
                status,
                rough_outcome,
                light_followup_by_event.get(event_id),
            )
            buckets[public_state] += 1
            if public_state == "insufficient" and rough_outcome == "ROUGH_INSUFFICIENT":
                insufficient_breakdown["rough_review"] += 1
            elif public_state == "insufficient" and status == "weak":
                insufficient_breakdown["canonical_weak_without_rough_insufficient"] += 1

        total = len(events)
        partition_total = sum(buckets.values())
        return {
            "schema_version": 1,
            "total": total,
            **buckets,
            "partition_total": partition_total,
            "partition_complete": partition_total == total,
            "insufficient_breakdown": insufficient_breakdown,
            "active_light_followups": len(light_followup_by_event),
            "light_followup_statuses": {
                followup_status: sum(
                    1 for value in light_followup_by_event.values() if value == followup_status
                )
                for followup_status in ("PENDING_EVIDENCE_REVIEW", "PENDING_HUMAN_REVIEW")
            },
            "definitions": {
                "verified": "formally verified canonical events",
                "excluded": "canonically rejected events",
                "insufficient": (
                    "canonical weak events or events whose latest authorized rough review "
                    "concluded ROUGH_INSUFFICIENT"
                ),
                "pending_verification": (
                    "candidate events without a completed rough-review disposition or any event with an "
                    "active evidence/human light-verification follow-up"
                ),
                "rough_reviewed": (
                    "candidate events with an authorized rough review completed without an "
                    "insufficient outcome; not formal verification"
                ),
            },
        }

    @staticmethod
    def _reader_quality(connection: sqlite3.Connection) -> dict[str, Any]:
        """Measure formal-claim citation quality across the canonical ledger.

        Citation readiness requires a named subject, a structured statement of
        what happened, and a citable source passage.  A false value limits how
        the claim may be presented; it never hides the event from Public.
        """

        rows = connection.execute(
            PUBLIC_EVENT_STATE_CTE
            + """
               SELECT public_state,
                      COUNT(*) AS total,
                      SUM(reader_ready) AS reader_ready,
                      SUM(CASE WHEN reader_has_subject=0 THEN 1 ELSE 0 END)
                        AS missing_subject,
                      SUM(CASE WHEN reader_has_fact_summary=0 THEN 1 ELSE 0 END)
                        AS missing_fact_summary,
                      SUM(CASE WHEN citable_evidence_count=0 THEN 1 ELSE 0 END)
                        AS missing_citable_evidence
               FROM event_public
               GROUP BY public_state"""
        ).fetchall()
        state_counts = {
            "verified": 0,
            "excluded": 0,
            "insufficient": 0,
            "pending_verification": 0,
            "rough_reviewed": 0,
        }
        total = 0
        ready = 0
        missing_subject = 0
        missing_fact_summary = 0
        missing_citable_evidence = 0
        for row in rows:
            state = str(row["public_state"])
            state_ready = int(row["reader_ready"] or 0)
            if state in state_counts:
                state_counts[state] = state_ready
            total += int(row["total"] or 0)
            ready += state_ready
            missing_subject += int(row["missing_subject"] or 0)
            missing_fact_summary += int(row["missing_fact_summary"] or 0)
            missing_citable_evidence += int(row["missing_citable_evidence"] or 0)
        non_citation_ready_inventory = max(0, total - ready)
        return {
            "schema_version": 1,
            "definition": (
                "named subject + subject-action-stage-known_at fact + current supported P0/P1 passage"
            ),
            "total": total,
            "citation_ready": ready,
            "non_citation_ready_inventory": non_citation_ready_inventory,
            # Deprecated aliases retained for clients migrating from the
            # former reader-gate vocabulary.
            "reader_ready": ready,
            "discovery_only": non_citation_ready_inventory,
            "inventory_contract": {
                "citation_ready": {
                    "authoritative": True,
                    "definition": (
                        "canonical events whose current version may support a formal claim"
                    ),
                },
                "non_citation_ready_inventory": {
                    "authoritative": True,
                    "definition": (
                        "canonical events whose current version is not citation-ready; "
                        "they remain publicly browseable"
                    ),
                },
                "reader_ready": {
                    "deprecated": True,
                    "replacement": "citation_ready",
                },
                "discovery_only": {
                    "deprecated": True,
                    "replacement": "non_citation_ready_inventory",
                },
            },
            "gap_counts_nonexclusive": {
                "missing_subject": missing_subject,
                "missing_fact_summary": missing_fact_summary,
                "missing_citable_evidence": missing_citable_evidence,
            },
            "reader_funnel": {
                "schema_version": 1,
                "total": ready,
                **state_counts,
                "partition_total": sum(state_counts.values()),
                "partition_complete": sum(state_counts.values()) == ready,
                "definition": "current-version evidence-supported reader subset of the canonical ledger",
            },
            "read_only": True,
            "canonical_mutation": False,
        }

    def list_source_health(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            sources = [dict(row) for row in connection.execute("SELECT * FROM sources ORDER BY authority_tier, name")]
            cursors = [dict(row) for row in connection.execute("SELECT * FROM source_cursors ORDER BY source_id, cursor_type")]
            observation_stats = {
                row["source_id"]: {
                    "count": int(row["n"]),
                    "latest": row["latest"],
                }
                for row in connection.execute(
                    """SELECT source_id,COUNT(*) AS n,MAX(local_received_at) AS latest
                       FROM raw_observations GROUP BY source_id"""
                )
            }
        by_source: dict[str, list[dict[str, Any]]] = {}
        for cursor in cursors:
            by_source.setdefault(cursor["source_id"], []).append(cursor)
        result = []
        for source in sources:
            source_cursors = by_source.get(source["source_id"], [])
            latest = max(source_cursors, key=lambda item: item.get("updated_at") or "", default=None)
            stats = observation_stats.get(source["source_id"], {"count": 0, "latest": None})
            source["observations"] = stats["count"]
            source["cursor_status"] = (
                latest.get("status")
                if latest
                else "STATIC_IMPORTED" if stats["count"] else "REGISTERED_ONLY"
            )
            source["last_polled_at"] = latest.get("last_polled_at") if latest else None
            source["last_success_at"] = latest.get("last_success_at") if latest else stats["latest"]
            source["last_error"] = latest.get("last_error") if latest else None
            source["cursors"] = source_cursors
            result.append(source)
        return result

    def list_events(
        self,
        *,
        status: str | None = None,
        public_state: str | None = None,
        family: str | None = None,
        source: str | None = None,
        query: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        reader_ready: bool | None = None,
        captured_source_required: bool = False,
        exclude_nonfinancial_retractions: bool = False,
        source_excerpt_chars: int | None = None,
        sort: str = "event_date",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        allowed_public_states = {
            "verified",
            "excluded",
            "insufficient",
            "pending_verification",
            "rough_reviewed",
        }
        if public_state and public_state not in allowed_public_states:
            raise ValueError(f"unsupported public_state: {public_state}")
        sort_orders = {
            "event_date": "e.event_date DESC, e.last_updated_at DESC, e.event_id DESC",
            "latest": "e.last_updated_at DESC, e.event_date DESC, e.event_id DESC",
            "subject": (
                "LOWER(COALESCE(e.company_name,e.ticker_at_event,e.event_id)) ASC, "
                "e.event_date DESC, e.event_id ASC"
            ),
        }
        if sort not in sort_orders:
            raise ValueError(f"unsupported sort: {sort}")
        for name, value in (("date_from", date_from), ("date_to", date_to)):
            if value is None:
                continue
            try:
                parsed_date = date.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be YYYY-MM-DD") from exc
            if parsed_date.isoformat() != value:
                raise ValueError(f"{name} must be YYYY-MM-DD")
        if date_from and date_to and date_from > date_to:
            raise ValueError("date_from must not be after date_to")

        where: list[str] = []
        params: list[Any] = []
        if exclude_nonfinancial_retractions:
            where.append(
                "NOT EXISTS (SELECT 1 FROM event_versions vnoise "
                "WHERE vnoise.event_id=e.event_id "
                "AND vnoise.version=e.current_version "
                "AND vnoise.change_reason='official_nonfinancial_notice')"
            )
        if status:
            where.append("e.status=?")
            params.append(status)
        if public_state:
            where.append("e.public_state=?")
            params.append(public_state)
        if family:
            where.append("e.event_family=?")
            params.append(family)
        if source:
            where.append("e.discovery_source=?")
            params.append(source)
        if date_from:
            where.append("e.event_date>=?")
            params.append(date_from)
        if date_to:
            where.append("e.event_date<=?")
            params.append(date_to)
        if reader_ready is not None:
            where.append("e.reader_ready=?")
            params.append(int(reader_ready))
        if captured_source_required:
            where.append(
                "EXISTS (SELECT 1 FROM event_observations ceo "
                "JOIN latest_source_content csr ON csr.observation_id=ceo.observation_id "
                "WHERE ceo.event_id=e.event_id AND csr.observation_status!='deleted')"
            )
        if query:
            source_relation_filter = (
                "" if captured_source_required else "AND qeo.relation_type!='filtered_aggregated_noise' "
            )
            where.append(
                "(LOWER(COALESCE(e.company_name,'') || ' ' || COALESCE(e.ticker_at_event,'') || ' ' || "
                "e.event_type || ' ' || COALESCE(e.event_family,'') || ' ' || "
                "COALESCE(e.discovery_source,'') || ' ' || e.event_id) LIKE ? OR EXISTS ("
                "SELECT 1 FROM event_observations qeo JOIN latest_source_content qr "
                "ON qr.observation_id=qeo.observation_id WHERE qeo.event_id=e.event_id "
                f"{source_relation_filter}"
                "AND LOWER(COALESCE(qr.title,'') || ' ' || COALESCE(qr.summary,'')) LIKE ?))"
            )
            params.extend([f"%{query.lower()}%", f"%{query.lower()}%"])
        where_sql = " WHERE " + " AND ".join(where) if where else ""

        if public_state is None and reader_ready is None:
            page_state_cte = _page_scoped_public_event_state_cte(
                where_sql=where_sql,
                sort_sql=sort_orders[sort],
                source_excerpt_chars=source_excerpt_chars,
            )
            page_query = (
                page_state_cte
                + f"""
                SELECT e.*,
                       (SELECT COUNT(*) FROM event_evidence x WHERE x.event_id=e.event_id) AS evidence_count,
                       COALESCE((
                         SELECT rollup.captured_source_count
                         FROM event_source_rollup rollup
                         WHERE rollup.event_id=e.event_id
                       ),0) AS captured_source_count,
                       (SELECT severity_grade FROM event_assessments a WHERE a.event_id=e.event_id ORDER BY a.created_at DESC LIMIT 1) AS severity_grade,
                       (SELECT credibility_tier FROM event_assessments a WHERE a.event_id=e.event_id ORDER BY a.created_at DESC LIMIT 1) AS credibility_tier,
                       (SELECT evidence_passage FROM event_evidence x WHERE x.event_id=e.event_id AND evidence_passage IS NOT NULL ORDER BY passage_score DESC, updated_at DESC LIMIT 1) AS evidence_excerpt,
                       (SELECT r.title FROM ranked_event_sources r
                        WHERE r.event_id=e.event_id AND r.source_rank=1) AS source_title,
                       (SELECT r.summary FROM ranked_event_sources r
                        WHERE r.event_id=e.event_id AND r.source_rank=1) AS source_summary
                       ,(SELECT r.source_name FROM ranked_event_sources r
                         WHERE r.event_id=e.event_id AND r.source_rank=1) AS source_name
                FROM event_public e
                ORDER BY {sort_orders[sort]}
                """
            )
            with closing(self.connect()) as connection:
                # Keep count and items on one SQLite read snapshot.  The worker may
                # append events concurrently while the public page is loading.
                connection.execute("BEGIN")
                try:
                    total = int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM canonical_events e {where_sql}",
                            params,
                        ).fetchone()[0]
                    )
                    rows = connection.execute(
                        page_query,
                        [*params, limit, offset],
                    ).fetchall()
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return {
                "items": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
                "public_state": public_state,
                "date_from": date_from,
                "date_to": date_to,
                "reader_ready": reader_ready,
                "captured_source_required": captured_source_required,
                "exclude_nonfinancial_retractions": exclude_nonfinancial_retractions,
                "sort": sort,
            }

        paged_query = (
            PUBLIC_EVENT_STATE_CTE
            + f"""
            , paged_events AS (
                SELECT e.*,COUNT(*) OVER () AS _filtered_total
                FROM event_public e
                {where_sql}
                ORDER BY {sort_orders[sort]}
                LIMIT ? OFFSET ?
            )
            SELECT e.*,
                   (SELECT COUNT(*) FROM event_evidence x WHERE x.event_id=e.event_id) AS evidence_count,
                   (SELECT COUNT(*) FROM event_observations xeo
                    JOIN latest_source_content xr ON xr.observation_id=xeo.observation_id
                    WHERE xeo.event_id=e.event_id AND xr.observation_status!='deleted')
                     AS captured_source_count,
                   (SELECT severity_grade FROM event_assessments a WHERE a.event_id=e.event_id ORDER BY a.created_at DESC LIMIT 1) AS severity_grade,
                   (SELECT credibility_tier FROM event_assessments a WHERE a.event_id=e.event_id ORDER BY a.created_at DESC LIMIT 1) AS credibility_tier,
                   (SELECT evidence_passage FROM event_evidence x WHERE x.event_id=e.event_id AND evidence_passage IS NOT NULL ORDER BY passage_score DESC, updated_at DESC LIMIT 1) AS evidence_excerpt,
                   (SELECT r.title FROM event_observations eo
                    JOIN latest_source_content r ON r.observation_id=eo.observation_id
                    WHERE eo.event_id=e.event_id
                      AND eo.relation_type!='filtered_aggregated_noise'
                      AND r.observation_status!='deleted'
                    ORDER BY r.local_received_at DESC,r.observation_id DESC LIMIT 1) AS source_title,
                   (SELECT r.summary FROM event_observations eo
                    JOIN latest_source_content r ON r.observation_id=eo.observation_id
                    WHERE eo.event_id=e.event_id
                      AND eo.relation_type!='filtered_aggregated_noise'
                      AND r.observation_status!='deleted'
                    ORDER BY r.local_received_at DESC,r.observation_id DESC LIMIT 1) AS source_summary,
                   (SELECT source.name FROM event_observations eo
                    JOIN latest_source_content r ON r.observation_id=eo.observation_id
                    JOIN sources source ON source.source_id=r.source_id
                    WHERE eo.event_id=e.event_id
                      AND eo.relation_type!='filtered_aggregated_noise'
                      AND r.observation_status!='deleted'
                    ORDER BY r.local_received_at DESC,r.observation_id DESC LIMIT 1) AS source_name
            FROM paged_events e
            ORDER BY {sort_orders[sort]}
            """
        )
        with closing(self.connect()) as connection:
            rows = connection.execute(
                paged_query,
                [*params, limit, offset],
            ).fetchall()
            items = [dict(row) for row in rows]
            if items:
                total = int(items[0]["_filtered_total"])
                for item in items:
                    item.pop("_filtered_total", None)
            else:
                total = connection.execute(
                    PUBLIC_EVENT_STATE_CTE
                    + f" SELECT COUNT(*) FROM event_public e {where_sql}",
                    params,
                ).fetchone()[0]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "public_state": public_state,
            "date_from": date_from,
            "date_to": date_to,
            "reader_ready": reader_ready,
            "captured_source_required": captured_source_required,
            "exclude_nonfinancial_retractions": exclude_nonfinancial_retractions,
            "sort": sort,
        }

    def event_facets(
        self,
        *,
        reader_ready: bool | None = None,
        exclude_nonfinancial_retractions: bool = False,
    ) -> dict[str, Any]:
        """Return bounded, live filter suggestions without exposing event content."""
        source_table = "canonical_events" if reader_ready is None else "event_public"
        conditions = [] if reader_ready is None else ["e.reader_ready=?"]
        if exclude_nonfinancial_retractions:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM event_versions vnoise "
                "WHERE vnoise.event_id=e.event_id "
                "AND vnoise.version=e.current_version "
                "AND vnoise.change_reason='official_nonfinancial_notice')"
            )
        where = " AND " + " AND ".join(conditions) if conditions else ""
        params: tuple[Any, ...] = () if reader_ready is None else (int(reader_ready),)
        with closing(self.connect()) as connection:
            families = [
                {"value": row["value"], "count": int(row["n"])}
                for row in connection.execute(
                    ((PUBLIC_EVENT_STATE_CTE + " ") if reader_ready is not None else "")
                    + f"""SELECT event_family AS value, COUNT(*) AS n
                       FROM {source_table} e
                       WHERE e.event_family IS NOT NULL AND TRIM(e.event_family) != ''
                       {where}
                       GROUP BY e.event_family
                       ORDER BY n DESC, value ASC
                       LIMIT 100""",
                    params,
                )
            ]
            sources = [
                {"value": row["value"], "count": int(row["n"])}
                for row in connection.execute(
                    ((PUBLIC_EVENT_STATE_CTE + " ") if reader_ready is not None else "")
                    + f"""SELECT discovery_source AS value, COUNT(*) AS n
                       FROM {source_table} e
                       WHERE e.discovery_source IS NOT NULL AND TRIM(e.discovery_source) != ''
                       {where}
                       GROUP BY e.discovery_source
                       ORDER BY n DESC, value ASC
                       LIMIT 100""",
                    params,
                )
            ]
        return {
            "families": families,
            "sources": sources,
            "reader_ready": reader_ready,
            "exclude_nonfinancial_retractions": exclude_nonfinancial_retractions,
            "read_only": True,
            "no_trading": True,
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            # A detail read must not evaluate the public evidence contract for
            # every event in the ledger.  The public feed already uses this
            # page-scoped projection; bounding it to one canonical row keeps a
            # cold dossier read proportional to the selected event instead of
            # the full production history.
            event_state_cte = _page_scoped_public_event_state_cte(
                where_sql="WHERE e.event_id=?",
                sort_sql="e.event_id ASC",
            )
            event = _dict(
                connection.execute(
                    event_state_cte + " SELECT * FROM event_public",
                    (event_id, 1, 0),
                ).fetchone()
            )
            if event is None:
                return None
            event["captured_source_count"] = int(
                connection.execute(
                    """SELECT COUNT(*) FROM event_observations eo
                       JOIN latest_source_content r
                         ON r.observation_id=eo.observation_id
                       WHERE eo.event_id=? AND r.observation_status!='deleted'""",
                    (event_id,),
                ).fetchone()[0]
            )
            rough_row = connection.execute(
                """SELECT payload_json,updated_at
                   FROM pipeline_jobs
                   WHERE event_id=? AND status='COMPLETED_AUTHORIZED_ROUGH_REVIEW'
                   ORDER BY updated_at DESC,job_id DESC LIMIT 1""",
                (event_id,),
            ).fetchone()
            rough = (
                self._rough_review_metadata(rough_row["payload_json"], rough_row["updated_at"])
                if rough_row is not None
                else None
            )
            light_followup_row = connection.execute(
                """SELECT status,payload_json,updated_at
                   FROM pipeline_jobs
                   WHERE event_id=? AND job_type='light_verification_followup'
                     AND status IN ('PENDING_EVIDENCE_REVIEW','PENDING_HUMAN_REVIEW')
                   ORDER BY updated_at DESC,job_id DESC LIMIT 1""",
                (event_id,),
            ).fetchone()
            light_followup: dict[str, Any] | None = None
            if light_followup_row is not None:
                payload = _json(light_followup_row["payload_json"], {})
                details = (
                    payload.get("light_verification_followup")
                    if isinstance(payload, dict)
                    else None
                )
                details = details if isinstance(details, dict) else {}
                light_followup = {
                    "status": str(light_followup_row["status"]),
                    "updated_at": str(light_followup_row["updated_at"]),
                    "last_attempted_at": details.get("last_attempted_at"),
                    "expected_next_action": details.get("expected_next_action"),
                    "gap_reasons": details.get("gap_reasons", []),
                    "legacy_reconciliation": bool(details.get("legacy_reconciliation")),
                    "formal_verification": False,
                    "no_trading": True,
                }
            event["public_state"] = self._public_state(
                event.get("status"),
                str(rough["outcome"]) if rough else None,
                str(light_followup["status"]) if light_followup else None,
            )
            event["reviewed_at"] = rough.get("reviewed_at") if rough else None
            event["light_followup"] = light_followup
            version = _dict(
                connection.execute(
                    "SELECT * FROM event_versions WHERE event_id=? AND version=?",
                    (event_id, event["current_version"]),
                ).fetchone()
            )
            if version:
                version["facts"] = _json(version.pop("facts_json"), {})
            assessment = _dict(
                connection.execute(
                    "SELECT * FROM event_assessments WHERE event_id=? ORDER BY created_at DESC LIMIT 1",
                    (event_id,),
                ).fetchone()
            )
            assets = [
                dict(row)
                for row in connection.execute(
                    """SELECT i.*, a.asset_type, a.symbol, a.provider_symbol, a.currency,
                              a.venue, a.metadata_json,
                              receipt.display_role,receipt.proxy_label
                       FROM event_asset_impacts i
                       JOIN assets a ON a.asset_id=i.asset_id
                       LEFT JOIN event_asset_mapping_receipts receipt
                         ON receipt.mapping_decision_id=i.mapping_decision_id
                        AND receipt.event_id=i.event_id
                        AND receipt.asset_id=i.asset_id
                        AND receipt.relation_type=i.relation_type
                        AND receipt.decision='SELECTED'
                       WHERE i.event_id=? ORDER BY i.impact_score DESC""",
                    (event_id,),
                )
            ]
            for asset in assets:
                asset["reason_codes"] = _json(asset.pop("reason_codes_json"), [])
                asset["metadata"] = _json(asset.pop("metadata_json"), {})
            metrics = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM event_market_metrics
                        WHERE event_id=? AND event_version=? ORDER BY metric_name""",
                    (event_id, event["current_version"]),
                )
            ]
            snapshots = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM market_snapshots WHERE event_id=? ORDER BY captured_at DESC",
                    (event_id,),
                )
            ]
            market_jobs = [
                dict(row)
                for row in connection.execute(
                    """SELECT j.market_job_id,j.event_id,j.event_version,j.asset_id,j.provider,
                              j.observation_window,j.status,j.scheduled_at,j.completed_at,
                              j.attempts,j.last_error,j.no_trading,
                              anchor.event_version AS anchor_event_version,
                              anchor.declared_anchor_kind,anchor.reaction_anchor_at,
                              anchor.known_at,anchor.timestamp_precision,
                              anchor.anchor_status,anchor.reason_code AS anchor_reason_code,
                              anchor.unsupported_windows_json,
                              link.offset_seconds,link.window_contract_version
                       FROM market_jobs j
                       LEFT JOIN market_job_anchor_links link
                         ON link.market_job_id=j.market_job_id
                       LEFT JOIN market_event_anchors anchor
                         ON anchor.anchor_id=link.anchor_id
                       WHERE j.event_id=?
                       ORDER BY j.asset_id,j.scheduled_at,j.observation_window""",
                    (event_id,),
                )
            ]
            for job in market_jobs:
                job["unsupported_windows"] = _json(
                    job.pop("unsupported_windows_json"), []
                )
            preferred_source = _dict(
                connection.execute(
                    """SELECT r.title,r.summary,r.source_id,r.external_id,
                              r.canonical_url,r.content_sha256,r.raw_json,
                              r.source_published_at,r.local_received_at,
                              r.latest_revision_no,r.latest_revision_kind,
                              eo.observation_id,eo.relation_type,
                              source.name AS source_name,
                              source.source_type,source.authority_tier
                       FROM event_observations eo
                       JOIN latest_source_content r ON r.observation_id=eo.observation_id
                       JOIN sources source ON source.source_id=r.source_id
                       JOIN canonical_events current_event
                         ON current_event.event_id=eo.event_id
                       WHERE eo.event_id=?
                         AND eo.relation_type!='filtered_aggregated_noise'
                         AND r.observation_status!='deleted'
                       ORDER BY CASE
                                  WHEN DATE(r.source_published_at)=DATE(current_event.event_date)
                                   AND LENGTH(TRIM(COALESCE(r.summary,'')))>=40
                                  THEN 0 ELSE 1
                                END,
                                r.local_received_at DESC,r.observation_id DESC LIMIT 1""",
                    (event_id,),
                ).fetchone()
            )
        return {
            "event": event,
            "current_version": version,
            "assessment": assessment,
            "assets": assets,
            "market_metrics": metrics,
            "market_snapshots": snapshots,
            "market_jobs": market_jobs,
            "preferred_source": preferred_source,
        }

    def captured_sources(self, event_id: str) -> list[dict[str, Any]]:
        """Return every retained discovery capture, including filtered edges.

        These records explain what the collector actually received.  They are
        deliberately separate from ``event_evidence`` and never imply that a
        source supports the canonical event claim.
        """

        # This is deliberately event-scoped instead of reusing
        # ``_CURRENT_SOURCE_CONTENT_CTES``.  The shared CTE ranks every source
        # revision in the ledger before the outer event filter can run.  On a
        # production ledger that turns a one-capture dossier read into a
        # full-table window scan.  The correlated MAX lookup below uses the
        # existing ``(observation_id, revision_no)`` index and preserves the
        # exact latest-revision projection without touching unrelated events.
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT eo.relation_type,eo.linked_at,
                          r.observation_id,r.source_id,r.external_id,
                          CASE
                            WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
                            THEN COALESCE(
                              NULLIF(TRIM(json_extract(sr.raw_json,'$.item.published_at')),''),
                              NULLIF(TRIM(json_extract(sr.raw_json,'$.item.created_at')),''),
                              NULLIF(TRIM(json_extract(sr.raw_json,'$.item.posted_at')),''),
                              r.source_published_at
                            )
                            ELSE r.source_published_at
                          END AS source_published_at,
                          r.local_received_at,
                          COALESCE(sr.title,r.title) AS title,
                          COALESCE(sr.summary,r.summary) AS summary,
                          CASE
                            WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
                            THEN COALESCE(
                              NULLIF(TRIM(json_extract(sr.raw_json,'$.item.link')),''),
                              NULLIF(TRIM(json_extract(sr.raw_json,'$.item.url')),''),
                              r.canonical_url
                            )
                            ELSE r.canonical_url
                          END AS canonical_url,
                          COALESCE(sr.content_sha256,r.content_sha256) AS content_sha256,
                          COALESCE(sr.raw_json,r.raw_json) AS raw_json,
                          CASE WHEN sr.revision_kind='delete'
                               THEN 'deleted' ELSE r.observation_status END
                            AS observation_status,
                          COALESCE(sr.revision_no,0) AS latest_revision_no,
                          COALESCE(sr.revision_kind,'new') AS latest_revision_kind,
                          COALESCE(sr.revision_at,r.local_received_at) AS latest_revision_at,
                          s.name AS source_name,s.source_type,s.authority_tier
                   FROM event_observations eo
                   JOIN raw_observations r
                     ON r.observation_id=eo.observation_id
                   LEFT JOIN source_revisions sr
                     ON sr.observation_id=r.observation_id
                    AND sr.revision_no=(
                          SELECT MAX(sr2.revision_no)
                          FROM source_revisions sr2
                          WHERE sr2.observation_id=r.observation_id
                        )
                   JOIN sources s ON s.source_id=r.source_id
                   WHERE eo.event_id=?
                   ORDER BY CASE WHEN sr.revision_kind='delete'
                                      OR r.observation_status='deleted'
                                 THEN 1 ELSE 0 END,
                            r.local_received_at DESC,r.observation_id DESC""",
                (event_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_payload = str(item.get("raw_json") or "")
            item["raw_payload_sha256"] = hashlib.sha256(
                raw_payload.encode("utf-8")
            ).hexdigest()
            receipt_payload = {
                "source_id": item.get("source_id"),
                "external_id": item.get("external_id"),
                "semantic_content_sha256": item.get("content_sha256"),
                "canonical_url": item.get("canonical_url"),
                "source_published_at": item.get("source_published_at"),
                "local_received_at": item.get("local_received_at"),
                "latest_revision_no": item.get("latest_revision_no"),
                "latest_revision_kind": item.get("latest_revision_kind"),
                "raw_payload_sha256": item["raw_payload_sha256"],
            }
            item["capture_receipt_sha256"] = hashlib.sha256(
                json.dumps(
                    receipt_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            item["semantic_content_sha256"] = item.pop("content_sha256", None)
            result.append(item)
        return result

    def market_capabilities(self) -> dict[str, Any]:
        """Summarize observed read-only providers without exposing credentials."""
        registry = {
            "binance_public": {
                "name": "Binance Public Spot",
                "role": "PERSISTED_EVENT_OBSERVATION",
                "asset_classes": ["crypto"],
                "access": "PUBLIC_NONE_AUTH",
                "deployment": "SERVER_DIRECT",
                "activity_scope": "EVENT_TRIGGERED_SNAPSHOTS",
            },
            "twelve_data": {
                "name": "Twelve Data",
                "role": "PERSISTED_EVENT_OBSERVATION",
                "asset_classes": ["equity", "etf", "fx", "commodity_proxy"],
                "access": "API_KEY_MARKET_DATA_ONLY",
                "deployment": "SERVER_DIRECT",
                "activity_scope": "EVENT_TRIGGERED_SNAPSHOTS",
            },
            "ibkr_tws_readonly": {
                "name": "IBKR TWS Read-Only",
                "role": "CAPABILITY_PROBE_ONLY",
                "asset_classes": ["equity", "fx", "futures"],
                "access": "LOCAL_TWS_READ_ONLY",
                "deployment": "OPERATOR_DESKTOP",
                "activity_scope": "LOCAL_CAPABILITY_PROBE",
            },
        }
        with closing(self.connect()) as connection:
            job_rows = {
                row["provider"]: dict(row)
                for row in connection.execute(
                    """SELECT provider,COUNT(*) AS jobs,
                              SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS completed,
                              SUM(CASE WHEN status IN ('PENDING','RETRY') THEN 1 ELSE 0 END) AS pending,
                              SUM(CASE WHEN status IN ('RETRY','FAILED') AND last_error IS NOT NULL
                                       THEN 1 ELSE 0 END) AS errors,
                              MAX(completed_at) AS last_completed_at
                       FROM market_jobs GROUP BY provider"""
                )
            }
            snapshot_rows = {
                row["provider"]: dict(row)
                for row in connection.execute(
                    """SELECT provider,COUNT(*) AS snapshots,MAX(captured_at) AS last_snapshot_at
                       FROM market_snapshots GROUP BY provider"""
                )
            }
            latest_errors = {
                row["provider"]: row["last_error"]
                for row in connection.execute(
                    """SELECT j.provider,j.last_error FROM market_jobs j
                       JOIN (
                         SELECT provider,MAX(scheduled_at) AS scheduled_at
                         FROM market_jobs
                         WHERE status IN ('RETRY','FAILED') AND last_error IS NOT NULL
                         GROUP BY provider
                       ) latest ON latest.provider=j.provider AND latest.scheduled_at=j.scheduled_at
                       WHERE j.status IN ('RETRY','FAILED') AND j.last_error IS NOT NULL"""
                )
            }
            window_rows = connection.execute(
                """SELECT provider,observation_window,status,COUNT(*) AS count
                   FROM market_jobs
                   GROUP BY provider,observation_window,status
                   ORDER BY provider,observation_window,status"""
            ).fetchall()

        window_status: dict[str, dict[str, dict[str, int]]] = {}
        for row in window_rows:
            provider_windows = window_status.setdefault(row["provider"], {})
            statuses = provider_windows.setdefault(row["observation_window"], {})
            statuses[row["status"]] = int(row["count"])

        providers = []
        observed_at = datetime.now(timezone.utc)
        for provider_id, definition in registry.items():
            jobs = job_rows.get(provider_id, {})
            snapshots = snapshot_rows.get(provider_id, {})
            completed = int(jobs.get("completed") or 0)
            pending = int(jobs.get("pending") or 0)
            errors = int(jobs.get("errors") or 0)
            if provider_id == "ibkr_tws_readonly":
                status = "LOCAL_PROBE_ONLY"
            elif errors:
                status = "DEGRADED"
            elif completed:
                status = "OBSERVED"
            elif pending:
                status = "PENDING"
            else:
                status = "UNOBSERVED"
            last_snapshot_at = snapshots.get("last_snapshot_at")
            snapshot_age_seconds: int | None = None
            if last_snapshot_at:
                parsed = datetime.fromisoformat(str(last_snapshot_at).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                snapshot_age_seconds = max(0, int((observed_at - parsed.astimezone(timezone.utc)).total_seconds()))
            if provider_id == "ibkr_tws_readonly":
                freshness_status = "NOT_APPLICABLE_LOCAL_PROBE"
            elif snapshot_age_seconds is None:
                freshness_status = "NO_CAPTURE"
            elif snapshot_age_seconds <= 15 * 60:
                freshness_status = "FRESH_CAPTURE"
            elif snapshot_age_seconds <= 24 * 60 * 60:
                freshness_status = "RECENT_EVENT_CAPTURE"
            else:
                freshness_status = "STALE_EVENT_CAPTURE"
            providers.append(
                {
                    "provider_id": provider_id,
                    **definition,
                    "status": status,
                    "jobs": int(jobs.get("jobs") or 0),
                    "completed_jobs": completed,
                    "pending_jobs": pending,
                    "snapshots": int(snapshots.get("snapshots") or 0),
                    "last_snapshot_at": last_snapshot_at,
                    "snapshot_age_seconds": snapshot_age_seconds,
                    "freshness_status": freshness_status,
                    "continuous_feed": False,
                    "last_error": latest_errors.get(provider_id),
                    "observation_windows": window_status.get(provider_id, {}),
                    "read_only": True,
                    "account_data_used": False,
                    "order_endpoints_present": False,
                }
            )
        return {
            "providers": providers,
            "provider_policy": {
                "crypto": "binance_public",
                "non_crypto": "twelve_data",
                "ibkr": "local_capability_probe_only",
            },
            "horizon_policy": {
                "baseline": "version_bound_exact_event_anchor",
                "anchor_contract": "market-anchor-v1",
                "known_at_rule": "max_source_published_at_local_received_at",
                "windows": [
                    "t_plus_5m",
                    "t_plus_30m",
                    "t_plus_2h",
                    "next_close",
                    "t_plus_1d",
                    "t_plus_5d",
                ],
                "missed_window_behavior": "record_MISSED_WINDOW_without_latest_quote_substitution",
                "return_metric_scope": "post_event_audit_only",
                "continuous_quote_feed": False,
                "freshness_disclosure": "provider capability and event-triggered snapshot freshness are reported separately",
            },
            "boundary": {
                "read_only": True,
                "no_trading": True,
                "account_data_used": False,
                "post_event_audit_only": True,
                "allowed_as_model_feature": False,
            },
        }

    def evidence_snapshot_eligible_pairs(self) -> set[tuple[str, str]]:
        placeholders = ",".join("?" for _ in EVIDENCE_SNAPSHOT_SOURCE_IDS)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT ev.event_id,ev.evidence_id
                    FROM event_evidence ev
                    JOIN raw_observations r ON r.observation_id=ev.observation_id
                    WHERE r.source_id IN ({placeholders})
                      AND ev.evidence_url IS NOT NULL AND TRIM(ev.evidence_url)!=''""",
                EVIDENCE_SNAPSHOT_SOURCE_IDS,
            ).fetchall()
        return {(str(row["event_id"]), str(row["evidence_id"])) for row in rows}

    def evidence_snapshot_eligibility(self) -> dict[str, Any]:
        return {
            "eligible_links": len(self.evidence_snapshot_eligible_pairs()),
            "policy": "registered_official_sources_with_nonempty_evidence_url",
        }

    def event_evidence(self, event_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""WITH {_EVENT_SCOPED_SOURCE_CONTENT_CTES},
                    {_EVENT_SCOPED_SEC_CURRENT_SUPPORTED_FACT_SLOT_CTE}
                    SELECT ev.*, ro.title AS observation_title, ro.summary AS observation_summary,
                           ro.source_published_at,ro.local_received_at,ro.content_sha256,
                           ro.observation_status,ro.latest_revision_no,ro.latest_revision_kind,
                           src.source_id,src.name AS source_name,
                           src.authority_tier,src.source_type,
                           rel.event_version AS relation_event_version,
                           rel.relation_status,rel.subject_match,rel.event_claim_supported,
                           rel.date_coherent,rel.modality,rel.evidence_fingerprint,
                           rel.contract_version AS relation_contract_version,
                           CASE WHEN ev.evidence_status='accepted_dual_human_primary_evidence'
                                  AND rel.relation_status='HUMAN_CONFIRMED'
                                  AND {_DUAL_HUMAN_RECEIPT_MATCH_SQL}
                                THEN 1 ELSE 0 END AS dual_human_receipt_consistent,
                           CASE WHEN
                             (
                               (
                                 ev.evidence_status IN (
                                   'machine_extracted_unreviewed','candidate_passage',
                                   'confirmed_primary','accepted_manual_primary_evidence',
                                   'accepted_light_primary_evidence'
                                 )
                                 AND rel.relation_status IN ('SCOPED_MATCH','HUMAN_CONFIRMED')
                                 AND {_SEC_CURRENT_FACT_SLOT_MATCH_SQL}
                                 AND rel.contract_version='event-admission-v3'
                                 AND rel.evidence_fingerprint=json_extract(
                                       current_version.facts_json,'$.evidence_fingerprint'
                                     )
                               ) OR (
                                 ev.evidence_status='accepted_dual_human_primary_evidence'
                                 AND rel.relation_status='HUMAN_CONFIRMED'
                                 AND {_DUAL_HUMAN_RECEIPT_MATCH_SQL}
                               )
                             )
                             AND rel.subject_match=1
                             AND rel.event_claim_supported=1
                             AND rel.date_coherent=1
                             AND rel.event_version=ce.current_version
                             AND {_CURRENT_EVIDENCE_PASSAGE_MATCH_SQL}
                             AND TRIM(COALESCE(ev.evidence_url,''))!=''
                             AND LENGTH(TRIM(COALESCE(ev.evidence_passage,'')))>=40
                             AND (
                               UPPER(src.authority_tier) IN ('P0','P1')
                               OR UPPER(src.authority_tier) GLOB 'P0_*'
                               OR UPPER(src.authority_tier) GLOB 'P1_*'
                             )
                           THEN 1 ELSE 0 END AS reader_eligible
                    FROM selected_event_evidence ev
                    JOIN current_source_content ro ON ro.observation_id=ev.observation_id
                    JOIN sources src ON src.source_id=ro.source_id
                    LEFT JOIN canonical_events ce ON ce.event_id=ev.event_id
                    LEFT JOIN event_versions current_version
                      ON current_version.event_id=ce.event_id
                     AND current_version.version=ce.current_version
                    LEFT JOIN sec_current_supported_fact_slots sec_slot
                      ON sec_slot.event_id=ce.event_id
                     AND sec_slot.version=ce.current_version
                    LEFT JOIN event_evidence_relations rel
                      ON rel.event_id=ev.event_id AND rel.evidence_id=ev.evidence_id
                     AND rel.event_version=ce.current_version
                   ORDER BY reader_eligible DESC,
                            CASE
                              WHEN UPPER(src.authority_tier)='P0'
                                OR UPPER(src.authority_tier) GLOB 'P0_*' THEN 0
                              WHEN UPPER(src.authority_tier)='P1'
                                OR UPPER(src.authority_tier) GLOB 'P1_*' THEN 1
                              ELSE 9
                            END,
                            ev.passage_score DESC,ev.updated_at DESC,ev.evidence_id""",
                (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def event_timeline(self, event_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            entries: list[dict[str, Any]] = []
            for row in connection.execute(
                "SELECT * FROM event_versions WHERE event_id=? ORDER BY version", (event_id,)
            ):
                item = dict(row)
                item["facts"] = _json(item.pop("facts_json"), {})
                entries.append({"at": item["changed_at"], "kind": "event_version", "payload": item})
            for row in connection.execute(
                """SELECT o.observation_id,o.source_id,o.source_published_at,o.local_received_at,
                          o.title,o.canonical_url,eo.relation_type,eo.linked_at
                   FROM event_observations eo JOIN raw_observations o ON o.observation_id=eo.observation_id
                   WHERE eo.event_id=?""",
                (event_id,),
            ):
                item = dict(row)
                entries.append({"at": item["local_received_at"], "kind": "observation", "payload": item})
            for row in connection.execute(
                "SELECT * FROM event_assessments WHERE event_id=?", (event_id,)
            ):
                item = dict(row)
                entries.append({"at": item["created_at"], "kind": "assessment", "payload": item})
        return sorted(entries, key=lambda item: item["at"] or "")

    def event_trace(self, event_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            jobs = [dict(row) for row in connection.execute("SELECT * FROM pipeline_jobs WHERE event_id=? ORDER BY created_at", (event_id,))]
            alerts = [dict(row) for row in connection.execute("SELECT * FROM alert_outbox WHERE event_id=? ORDER BY created_at", (event_id,))]
            for item in jobs:
                item["payload"] = _json(item.pop("payload_json"), {})
            for item in alerts:
                item["payload"] = _json(item.pop("payload_json"), {})
        return {"pipeline_jobs": jobs, "alerts": alerts}
