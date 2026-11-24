"""Mock EMR with three patients, one per outcome. Synthetic data only.

Shapes loosely follow FHIR (Patient, Condition, ServiceRequest, DocumentReference)
so it maps cleanly to a Da Vinci PAS / CMS-0057-F Prior Authorization API story.
"""

PATIENTS = {
    # Case A -> APPROVED on first pass (full documentation sent up front)
    "pt-1001": {
        "name": "Maria Lopez (synthetic)",
        "dob": "1971-03-14",
        "member_id": "HLT-88231",
        "conditions": [{"code": "M54.16", "system": "ICD-10", "display": "Lumbar radiculopathy"}],
        "service_request": {"code": "72148", "system": "CPT", "display": "MRI lumbar spine w/o contrast"},
        "documents": [
            {"type": "progress-note", "date": "2026-11-12",
             "text": "Radicular pain L5 distribution x 9 weeks. Positive straight leg raise. No red flags."},
            {"type": "pt-notes", "date": "2026-11-05",
             "text": "Physical therapy 2x/week for 7 weeks. Minimal improvement.",
             "conservative_therapy_weeks": 7},
        ],
        "red_flags": [],
    },
    # Case B -> PENDED for missing PT notes, then APPROVED after resubmission
    "pt-1002": {
        "name": "James Carter (synthetic)",
        "dob": "1965-08-02",
        "member_id": "HLT-44019",
        "conditions": [{"code": "M54.16", "system": "ICD-10", "display": "Lumbar radiculopathy"}],
        "service_request": {"code": "72148", "system": "CPT", "display": "MRI lumbar spine w/o contrast"},
        "documents": [
            {"type": "progress-note", "date": "2026-11-15",
             "text": "Persistent radicular pain x 8 weeks despite therapy. Considering surgical consult."},
            # PT notes live in the chart but are NOT sent initially (minimum necessary)
            {"type": "pt-notes", "date": "2026-11-10",
             "text": "Completed 6 weeks of PT and NSAIDs. Symptoms unchanged.",
             "conservative_therapy_weeks": 6},
        ],
        "red_flags": [],
        "initially_withhold": ["pt-notes"],
    },
    # Case C -> payer agent PROPOSES DENIAL -> clinician reviewer decides
    "pt-1003": {
        "name": "Aisha Khan (synthetic)",
        "dob": "1988-11-30",
        "member_id": "HLT-77502",
        "conditions": [{"code": "M54.50", "system": "ICD-10", "display": "Low back pain, unspecified"}],
        "service_request": {"code": "72148", "system": "CPT", "display": "MRI lumbar spine w/o contrast"},
        "documents": [
            {"type": "progress-note", "date": "2026-11-17",
             "text": "Acute low back pain x 10 days after lifting. No neuro deficits. No red flags. No therapy yet.",
             "conservative_therapy_weeks": 0},
        ],
        "red_flags": [],
    },
}


def get_patient(patient_id: str) -> dict:
    return PATIENTS[patient_id]


def find_documents(patient_id: str, doc_type: str) -> list[dict]:
    return [d for d in PATIENTS[patient_id]["documents"] if d["type"] == doc_type]
