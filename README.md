
# Port-2-Door

**AI-powered shipment tracking for importers and clearing agents in Nigeria.**

Shipment stage  tracking for importers and customs clearing agents in Nigeria, from the port (for example Onne) to the importer's address and the return of the empty container.

Built solo during the Meta Global AI Developer Hackathon (27 October to 4 November 2026).

**Demo video:** https://www.youtube.com/watch?v=a-XNKCHjDos

## The problem

Shipments are tracked from their country of origin to the destination port. After that, from the port to the importer's final address, everything goes blank. There is no transparency.

Importers are left in the dark. They do not know what is happening to their cargo or which stage it has reached, so they keep phoning their agents, and some agents are not straightforward with them. This often leads to disagreements between importers and their agents. When a container incurs demurrage, importers blame the agent and the agent blames the importer. When there is a delay, nobody can say whose it is, and some importers also do not pay on time.

What was before? Phone calls, emails and whatsapp messages.

## The solution

Port-2-Door gives both sides one shared, timestamped record of what has happened to a shipment.

- **Importers** need no account. They enter a container or bill of lading number and a code from their agent, and from the comfort of their home they see which stage the cargo has reached, at any time during clearing. The code is saved on their device. They can read each step in English or Nigerian Pidgin, and have it read aloud.
- **Agents** do almost no typing. They scan a bill of lading, or tell an AI assistant in plain words what happened, by typing or speaking. The assistant proposes the change, and **nothing is saved until the agent confirms it**. Fixed clearing rules decide what is allowed, not the model.
- **Accountability for both.** Every step is stamped with the time it was recorded and who recorded it, and a shipment can show who it is waiting on (customs, terminal, shipping line, the importer or the agent). Agents know they must update regularly, and an importer with a slow agent can see it and switch. If a delay or a demurrage charge happens, both sides can see where it sits.

## What uses Meta models

Every model this project calls while running is a Meta model.

| Feature | Meta model | Code |
|---|---|---|
| Bill of lading scan: reads a photo and pre-fills the shipment form | Muse Spark (`muse-spark-1.3`), vision | `tracking/ai/extract_bl.py` |
| Assistant: finds the shipment, proposes stage updates with tools, asks a question when unsure | Muse Spark (`muse-spark-1.3`), tool calling | `tracking/ai/assistant.py`, `tracking/ai/assistant_tools.py` |
| Voice input: speech to text for the assistant | Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | `tracking/ai/transcribe.py` |
| Pidgin stage explanations: first drafts at build time, then reviewed and edited by a person | Muse Spark, or Muse Spark Open when a Baseten key is set | `python manage.py generate_pidgin` |

**Not model calls** (browser features and plain code): microphone recording in the browser, the "Listen" button (the browser's built-in speech voice; no Meta model does text-to-speech), the English and Pidgin switch, the stage rules, the container number check digit, access codes, rate limiting and the confirm step.

The importer pages call no model. They show stored data and stored text.

## The clearing rules the app enforces

| Step | Needs first | Recorded |
|---|---|---|
| Arrived at port | nothing | once for the whole shipment |
| Manifest available, then PAAR assessed, then Duty paid | the step before it | once for the whole shipment |
| Shipping line processed, Terminal processed | PAAR assessed | per container |
| Customs processed, Customs examination (when selected), En route to bonded terminal | Duty paid | per container |
| Gate out | the three processing steps, and customs examination when selected | per container |
| In transit, Delivered, Empty container returned | the step before it | per container |

A step can be undone only if no step that needs it is already done. Every change is written to a history log.

## Run it

You need Python 3.14 or newer and git.

```bash
git clone https://github.com/kumbemaster/container-tracker.git
cd container-tracker
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # then edit .env (see below)
python manage.py bootstrap_demo    # creates the database and invented demo data, prints the demo login
python manage.py runserver
```

Open http://127.0.0.1:8000. Use localhost, because the microphone only works on localhost or HTTPS. Debug mode is on by default for local use only; for anything else set `DJANGO_DEBUG=0` and a long random `DJANGO_SECRET_KEY`.

**Keys.** `.env` needs a `DJANGO_SECRET_KEY` (any long random text) and, for the AI features, a Meta API key as `MODEL_API_KEY`. The app runs without a Meta key: the scan, the assistant and voice then say they are unavailable, and stages can still be updated with the buttons.

**Demo data** is all invented. `bootstrap_demo` prints the agent login and the importer access codes. The folder `samples/` has invented bills of lading for the scan: a clean image, a phone-style photo, a dense carrier-style BL with six containers and a "to order" consignee, a photo of it, a grey skewed scan, PDF versions of two of them, and a two-page text PDF whose container list is on an attached sheet.

## Try it

1. **Importer side.** On the home page search `CSQU3054383` with the code printed by `bootstrap_demo` for Demo Importer A. Open the steps, switch to Pidgin, and press Listen. Search `DEMOBL001` to see a shipment with two containers: searching a bill of lading shows the BL number, and searching one container shows all containers of the shipment without it.
2. **Agent side.** Go to `/agent/login/` and sign in with the demo login, or create your own account. Open **Shipments**.
3. **Scan.** Choose **Scan a BL** and upload one of the files in `samples/` (JPEG, PNG or PDF, up to 4 MB). Check the pre-filled form (doubtful fields are marked "Check this"), then save.
4. **Assistant.** On a shipment's page type or speak: `manifest, PAAR assessed and duty paid`. Review the confirm card, then confirm. Open the importer page in a private window and watch the timeline change.
5. **Rules.** Ask for `gate out` before the processing steps are done and see the assistant explain why it cannot.

## Tests

```bash
python manage.py test tracking
```

All model calls are mocked in the tests, so they need no key.

## Helper commands

| Command | What it does |
|---|---|
| `python manage.py bootstrap_demo` | Creates the demo data and the demo agent login |
| `python manage.py create_agent --username NAME --company "NAME"` | Creates an agent login (asks for the password) |
| `python manage.py scan_check samples/sample_bl_clean.png` | Tests the scan model call from the terminal |
| `python manage.py assistant_check --username NAME "sentence"` | Shows what the assistant would propose, without saving |
| `python manage.py transcribe_check file.wav` | Tests voice transcription on a WAV file |
| `python manage.py generate_pidgin` | Drafts the Pidgin texts for review |
| `python manage.py load_stage_explanations` | Loads the reviewed English and Pidgin texts |
| `python manage.py changepassword USERNAME` | Resets an agent's password (platform owner) |

## Safety and privacy

- Keys are read only from the environment and never stored in the repository.
- A model cannot save anything. It can only propose, and the server re-checks every proposal against the stage rules before it is applied.
- Importer pages never show importer names, agent company names or other shipments. A container search never shows the bill of lading number.
- Access codes are rate limited, and failures never reveal whether a shipment exists. Device access uses a random token whose hash is stored on the server.
- Agent accounts: sign-up always creates a new company and never joins an existing one; passwords are checked by Django's validators; logins, sign-up and password recovery are rate limited; password recovery uses a security question whose answer is stored hashed.
- Scanned documents are stored privately and served only to the agent's own company, as downloads, never as pages.
- Deleting a shipment hides it from the agent's list, from importers and from the assistant, and can be restored. Permanent deletion is available only to the company owner, needs a typed confirmation and the password, and only applies to shipments whose empty container has been returned or that never had any step recorded.
- Security headers are set, uploads are checked by content and size, and the code was reviewed against a security checklist. `pip-audit` found no known vulnerabilities in the pinned dependencies when it was run.

## Limits and what comes next

- No live connection to port or customs systems. Everything is recorded by the agent.
- Muse Voice Transcribe does not list Nigerian Pidgin among its evaluated languages, so voice input is for English. Pidgin appears as text on the importer side.
- A scan reads a JPEG, a PNG or the first five pages of a PDF, up to 4 MB, so an attached container list on page 2 or 3 is read too. PDFs are turned into page images inside the app process; a production deployment should do that in a sandboxed worker.
- Password recovery by security question is a stopgap for this demo; a real launch should use email or SMS recovery. Lockouts that stop guessing can also be used to lock a real user out on purpose, which a real launch would answer with a CAPTCHA.
- The security review was a code review, tests and a dependency audit, not a professional penetration test.
- Free-time clocks (terminal, shipping line and empty container return) are built and tested but hidden from importers by the setting `SHOW_FREE_TIME_CLOCKS`.
- Not built yet: scanning PAAR and customs query notes, a seal photo check, and an "ask about this container" assistant for importers.

## What was built during the event

All code in this repository was written between 27 October and 4 November 2026, with Muse Code as a coding assistant. The git history shows the work as it happened.

## Licence

MIT
