# Evidence analysis

[Documentation index](INDEX.md)

Corvus derives analytical views from the existing case evidence rather than treating graph relationships or grouped fields as new source facts.

## Accounts, timeline and graph

The unified Accounts view groups matching account records and retains source metadata. Distinguish account creation, modification, publication and observation dates. Use the Graph to inspect identifiers, sources and relationships; filters, layout and presentation exclusions do not delete underlying findings. Analyst annotations and manual relationships are distinct from source evidence.

## Image and location observations

Retained images can be compared using exact SHA-256 and perceptual dHash. Local YuNet/SFace matching generates optional face-similarity candidates, not identity assertions. Review candidates manually and retain rejected or contradictory matches. Reverse-image handoffs explicitly open an external provider and may disclose the selected image or its reference.

Location observations retain source, dates, precision and confidence. Profile-declared places and clustered observations are not proof of physical presence or residence. Respect privacy masking. Do not infer precise locations from obscured data.

## Historical and retained evidence

Wayback lookup and comparison present dated archive observations and historical changes. An archive timestamp is not an account creation date. Retained-artifact analysis operates locally on existing case files and can expose hashes and metadata without another collection request.

See [account evidence](MANUAL.md#9-account-evidence-and-correlation), [graph](MANUAL.md#16-investigation-graph), [location controls](MANUAL.md#location-intelligence-controls) and [raw evidence](MANUAL.md#18-raw-evidence-and-provenance).
