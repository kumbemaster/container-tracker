# Port-2-Door
Shipment tracking for importers and clearing agents (Meta hackathon project

Built during the Meta Global AI Developer Hackathon (27 Oct to 4 Nov 2026). Everything in this repository was created during the event.

## Running locally

Copy `.env.example` to `.env` and set `DJANGO_SECRET_KEY` to a long
random value. `DEBUG` is true by default for local use only; set
`DJANGO_DEBUG=0` (and a real secret key) for anything else. The AI
features need `MODEL_API_KEY`; without it they show as unavailable.
