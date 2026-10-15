# MISO Dutton J–Gaines 138 kV (monitored element 28264)

Reference history for explaining or forecasting congestion on this element. Compiled 2026-10-13
from the energycore-outages MCP (`get_constraint_drivers`, `get_outage_documentation`).

## Summary

Rare binder: two RT events in three years (2024-02-26, 2026-10-12). Both fit the same pattern:
**a 138 kV outage into Wealthy substation** weakens the path while the contingency is another
Wealthy line, combined with **high Consumers (CONS) load**. GAINES–MEADOWBK is background
loading only; outages at Hazelwood relieve.

- Binding direction: `<-1>` (ME name `DUTTONJ DUTTOGAINE13_1 1 <-1>`).
- Network model: monitored 18DUTTONJ1 – 18GAINES 1 (branch 36249), limit 180 MW (2026-03-01 case).
- No autoflow coefficients (not in the `miso` autoflow_constraints parameter).
- Only one plant has shift factors (J H Campbell, sf ≈ +0.03–0.05), so generator claims in
  notes (Zeeland, Ludington) cannot be checked from factors.

## Event 1: 2024-02-26 (RT constraint 219245, MET13413), $1,237, HE8–10

- Contingency: 18TALLMG – WEALTHY ckt 1.
- Driver: **138 TALLMADG–WEALTHY ckt 2 (60034075)**: out 07:13, binding 07:55–09:10.
  +13.6% of limit in combination (largest).
- Other loaders: NRIDG–TALLMADG +5.3%, KRAFT–SPAULDNG +3.0%, GAINES–MEADOWBK (60030441) +1.5%.
- Relieving: HAZELWD–TITUSLK −5.5%.
- Desk credited TALLMADG–WEALTHY + GAINES–MEADOWBK + high CONS load, and said GAINES ending
  (08:41) relieved it. The model gives GAINES little weight; the CONS load falling after the
  morning peak likely mattered as much.
- Annotation 3188 records the combination. TALLMADG–WEALTHY red-flagged 2026-07-31
  ("Loaded 28264 Dutton in 2/2024").

## In between

- 2025-08-14/15: speculative forecast when GAINES–MEADOWBK was scheduled again, paired with
  TALLMADG–WEALTHY (~50% FIDn), scaled down heavily. No recorded binding.
- 2026-10-07: GAINES–MEADOWBK flag updated: "Cannot drive congestion on its own".

## Event 2: 2026-10-12 (RT constraint 267206, MET13125), $3,333, 12:20–14:00 ($2,000 cap HE14)

- Contingency: BEALS_RD – WEALTHY.

| Outage | Status | In combination | Individual |
|---|---|---|---|
| **138 BEALS_RD–HAGERPK (60024808)** | out 10/09 12:45, **back 10/12 14:11** | **+11.6%** | +9.7% |
| 138 EPARIS–PLASTERC (16604) | Proposed only, not in active table | +4.3% | +5.1% |
| 138 GAINES–MEADOWBK (60030441) | out 10/01 → 2027-02-26 | +2.1% | +3.1% |
| 138 ARGENTA–HAZELWD (60044507) | out 10/12 06:14 | **−2.7%** | −3.5% |
| 138 BEALS_RD–DORRCORJ (60028893) | long-term | −0.8% | −2.1% |

- Timing check: BEALS_RD–HAGERPK returning at 14:11 is the only change that lines up with prices
  dropping from $2,000 to 0. It had **no flag, annotation, or note history**.
- All significant outages take model post-contingency flow from 39 to 62 MW (35% of the limit).
- MISO load rose about 7.2 GW from HE12 to HE14 (consistent with a CONS load push; no CONS zone data).

### The 10/12 desk note did not match the data

| Note claim | Data |
|---|---|
| GAINES–MEADOWBK ~45% PSI | +2–3% in the model; desk's own flag says it can't drive congestion alone |
| ARGENTA–HAZELWD ~9% loading | Relieves (−2.7% to −3.5%), the same as Hazelwood outages in 2024 |
| "together ~61% PSIn" | 35% of limit with all outages |
| (omitted) | BEALS_RD–HAGERPK, the outage most likely behind the binding |

## Open questions

1. Why no binding 10/09–10/11 while BEALS_RD–HAGERPK was out? Possibly lower weekend load
   (10/12 was a Monday); unconfirmed.
2. Confirm EPARIS–PLASTERC (16604, Proposed, 8/31–12/31) was not actually out.

## Forecasting guidance

- Treat any outage on a 138 kV line into Wealthy (TALLMADG–WEALTHY, BEALS_RD–HAGERPK,
  BEALS_RD–WEALTHY) as a primary loader for this element, especially on high-CONS-load weekdays.
- GAINES–MEADOWBK is out until 2027-02-26, so it adds about 2–3% of background loading until then.
- Hazelwood-area outages (HAZELWD–TITUSLK, ARGENTA–HAZELWD) relieve.
