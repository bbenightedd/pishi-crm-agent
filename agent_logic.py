import json
import os
from pathlib import Path

import anthropic
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

SHEET_ID = "1tkFlhw-zc3-jU9qfY4TGJ0-9Q2-4fScpKerfU_jYPRI"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WRITE_NEW_STATUS_IF_BLANK = True
RETRY_ON_JSON_FAILURE = True
FAILED_RESPONSE_DIR = Path("logs/failed_responses")

PROTECTED_STATUSES = {"Contacted", "Diagnostic Scheduled"}
AGENT_ALLOWED_FINAL_STATUSES = {"Assessed", "Declined"}

CRM_STATUS_COLUMN = "CRM status"
AGENT_SCORE_COLUMN = "Agent Score"
RATIONALE_COLUMN = "Rationale"
PROPOSED_TIER_COLUMN = "Proposed Tier"
FOLLOWUP_DRAFT_COLUMN = "Follow-up Draft"

creds = Credentials.from_service_account_file("service_account.json").with_scopes(SCOPES)
gc = gspread.authorize(creds)
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def load_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def get_system_instructions():
    persona = load_file("prompts/system_persona.md")
    core = load_file("context/pishi_strategic_core.md")
    icp = load_file("context/icp_definition.md")
    service_architecture = load_file("context/service_architecture.md")
    return f"{persona}\n\n{core}\n\n{icp}\n\n{service_architecture}"


def get_json_format_instructions():
    return (
        "Return exactly one valid JSON object and nothing else.\n"
        "Do not use markdown fences.\n"
        "Do not include commentary before or after the JSON.\n"
        "Escape all quotes inside string values.\n"
        "Use exactly these keys:\n"
        "{\n"
        '  "Agent Score": "...",\n'
        '  "Rationale": "...",\n'
        '  "Proposed Tier": "...",\n'
        '  "CRM Status": "Assessed" or "Declined",\n'
        '  "Follow-up Draft": "..."\n'
        "}"
    )


def analyze_lead(lead_text, retry_note=None):
    system_prompt = get_system_instructions()
    task_prompt = load_file("prompts/lead_qualifier.md")

    user_parts = [
        task_prompt,
        get_json_format_instructions(),
        f"LEAD DATA:\n{lead_text}",
    ]

    if retry_note:
        user_parts.insert(0, retry_note)

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": "\n\n".join(user_parts),
            }
        ],
    )

    text_blocks = [
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    ]
    return "".join(text_blocks).strip()




def extract_json_from_response(response_text):
    cleaned = strip_code_fences(response_text)

    start_index = cleaned.find("{")
    if start_index == -1:
        raise ValueError("No JSON object found in response.")

    decoder = json.JSONDecoder()
    parsed, _ = decoder.raw_decode(cleaned[start_index:])
    return parsed

def strip_code_fences(text):
    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()

        if lines and lines[0].startswith("```"):
    lines = lines[1:]

    if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()

    return cleaned

def write_failed_response(row_idx, full_name, response_text, error_message):
FAILED_RESPONSE_DIR.mkdir(parents=True, exist_ok=True)
safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in full_name)
output_path = FAILED_RESPONSE_DIR / f"row_{row_idx}_{safe_name}.txt"

with open(output_path, "w", encoding="utf-8") as f:
f.write(f"Row: {row_idx}\n")
f.write(f"Lead: {full_name}\n")
f.write(f"Error: {error_message}\n")
f.write("\n--- RAW RESPONSE ---\n")
f.write(response_text or "")

print(f"   Saved raw response to {output_path}")


def get_parsed_agent_output(lead_text, row_idx, full_name):
raw_response = analyze_lead(lead_text)

try:
return extract_json_from_response(raw_response), raw_response
except Exception as first_error:
write_failed_response(row_idx, full_name, raw_response, str(first_error))

if not RETRY_ON_JSON_FAILURE:
raise

print(f"   Retrying row {row_idx} with stricter JSON instruction...")

retry_note = (
"Your previous reply was not valid JSON. "
"Reply again with only one valid JSON object and no extra text."
)
retry_response = analyze_lead(lead_text, retry_note=retry_note)

try:
return extract_json_from_response(retry_response), retry_response
except Exception as second_error:
write_failed_response(row_idx, full_name, retry_response, str(second_error))
raise ValueError(
f"JSON parsing failed after retry. First error: {first_error}. "
f"Second error: {second_error}"
) from second_error


def normalize_crm_status(value):
return (value or "").strip()


def get_row_crm_status(row):
return normalize_crm_status(str(row.get(CRM_STATUS_COLUMN, "")))


def should_process_row(row):
crm_status = get_row_crm_status(row)
agent_score = str(row.get(AGENT_SCORE_COLUMN, "")).strip()
followup = str(row.get(FOLLOWUP_DRAFT_COLUMN, "")).strip()

if crm_status in PROTECTED_STATUSES:
return False

if agent_score or followup:
return False

if crm_status in {"", "New"}:
return True

return False


def build_lead_text(row):
return (
f"Full Name: {row.get('Full Name', '')}\n"
f"Business Name: {row.get('Business Name', '')}\n"
f"Email: {row.get('Email', '')}\n"
f"Website URL: {row.get('Website URL', '')}\n"
f"Primary Structural Concern: {row.get('Primary Structural Concern', '')}\n"
f"Lead Source: {row.get('Lead Source', '')}\n"
)


def parse_agent_output(parsed):
agent_score = str(parsed.get("Agent Score", "")).strip()
rationale = str(parsed.get("Rationale", "")).strip()
proposed_tier = str(parsed.get("Proposed Tier", "")).strip()
crm_status_new = str(
parsed.get("CRM status")
or parsed.get("CRM Status")
or ""
).strip()
followup_draft = str(parsed.get("Follow-up Draft", "")).strip()

required_fields = {
"Agent Score": agent_score,
"Rationale": rationale,
"Proposed Tier": proposed_tier,
"CRM Status": crm_status_new,
"Follow-up Draft": followup_draft,
}

missing_fields = [key for key, value in required_fields.items() if not value]
if missing_fields:
raise ValueError(f"Missing required fields in agent output: {missing_fields}")

if crm_status_new not in AGENT_ALLOWED_FINAL_STATUSES:
raise ValueError(
f"Invalid CRM status from model: '{crm_status_new}'. "
f"Must be one of: {sorted(AGENT_ALLOWED_FINAL_STATUSES)}"
)

return agent_score, rationale, proposed_tier, crm_status_new, followup_draft


def process_sheet():
if not ANTHROPIC_KEY:
raise EnvironmentError("Missing ANTHROPIC_API_KEY in environment.")

sh = gc.open_by_key(SHEET_ID)
worksheet = sh.sheet1
rows = worksheet.get_all_records()

print(f"Scanning {len(rows)} rows...")
processed_count = 0

for idx, row in enumerate(rows, start=2):
if not should_process_row(row):
continue

crm_status_current = get_row_crm_status(row)

if WRITE_NEW_STATUS_IF_BLANK and crm_status_current == "":
worksheet.update(range_name=f"K{idx}", values=[["New"]])
crm_status_current = "New"

lead_text = build_lead_text(row)
full_name = row.get("Full Name", "Unknown Lead")
print(f"-> Analyzing Row {idx}: {full_name}")

try:
parsed, raw_response = get_parsed_agent_output(lead_text, idx, full_name)

(
agent_score,
rationale,
proposed_tier,
crm_status_new,
followup_draft,
) = parse_agent_output(parsed)

current_status_cell = normalize_crm_status(
str(worksheet.acell(f"K{idx}").value or "")
)
if current_status_cell in PROTECTED_STATUSES:
print(f"   Skipped update: row moved to '{current_status_cell}' during run.")
continue

update_values = [[
agent_score,
rationale,
proposed_tier,
crm_status_new,
followup_draft,
]]

worksheet.update(
range_name=f"H{idx}:L{idx}",
values=update_values,
)
processed_count += 1
print(f"   Success: updated row {idx}")

except Exception as e:
print(f"   Error on row {idx}: {e}")

print(f"Done. Processed {processed_count} leads.")


if __name__ == "__main__":
process_sheet()
