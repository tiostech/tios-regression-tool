<!--
Instructions for the `binding_drivers` MCP prompt (see mcp_server.py).

This explains why the day's highest constraints bound, using the shift factor,
plant output, load, and tie flow data from lib/constraint_drivers.py together with
the desk's own quick notes. Edit this file to change what the report looks like.

Leave {{market}}, {{MARKET}} and {{date}} in place; the server fills them in. This
comment is not sent to the AI.
-->
<Steps>

Explain why the highest {{MARKET}} constraints bound on {{date}}.

1. Call `list_binding_constraints` with market `{{market}}`, date `{{date}}`, kind `rt`. Call it again with kind `da` to see day-ahead.
2. Pick the constraints that stand out: the largest totals, and any whose total or peak is large compared with their own history (`total_30d`, `max_day`), or that rarely bind (`days_bound`).
3. For each one, call `get_constraint_drivers` with its `constraint_id` and the matching `kind`.
4. Write one section per constraint, using the Output_Format below.

Use only the data that the tools return. If the data does not support a conclusion, say so.

</Steps>

<Reading_The_Data>

- **Generator impacts.** `load_mw` is the flow a plant's output change added to the constraint between the two hours: positive loads it, negative relieves it. A negative shift factor (`sf`) means more output from that plant loads the constraint.
- **Redispatch.** Once a constraint binds, the market moves generators to relieve it. Rows marked `possible_redispatch` (gas, coal, or oil moving in the relieving direction while binding) are probably a response to the constraint, not a cause. Do not cite them as relief that explains the price. Nuclear, solar, wind, hydro, and scheduled pumped storage do not respond this way, so their moves are causes.
- **Base hour.** Changes are measured from `base_hr`, ideally before binding started. If the constraint bound from HE1, say that the baseline is already a binding hour.
- **Autoflow.** The flow (MW) that zonal wind, solar, and load put on the constraint; + loads it. Compare its hourly shape with the shadow prices: if autoflow rises with congestion, regional conditions explain it (name the zones that moved most). If autoflow is flat while the price spikes, the cause is local (a plant, an outage) or not in the zonal data. The default source is pseudo actuals (latest forecast per hour, close to what happened); call `get_autoflow` with source `forecast` to see what the day-ahead market expected. If the coefficients come from another constraint ID, say so.
- **Generator sources.** Plant output comes from several vendors (`sources`). Where they disagree (`disagreement`), say which number you relied on; a single vendor's flat or zero values are often bad data.
- **Transmission outage impacts.** From EnergyCore's network model, including the constraint's contingency. `individual_pct` is the outage alone (like the desk's FIDi); `in_combination_pct` is what it adds on top of the other outages (closer to FIDn). Rows marked `check` usually mean the outages together split the model; do not rely on their in-combination number. The model is a fixed snapshot, so use these for which outages matter and roughly how much, not for exact flows. For MISO it includes mapped SPP and PJM outages. Outages only in the outage report and not in the active table may not actually be out.
- **Timing.** Binding spells come from 5-minute prices. An outage whose start or end lines up with binding starting, rising, or stopping, and that has a modeled impact, is strong evidence. The active table updates every 15 minutes, so allow that much slack.
- **EnergyCore matching and desk outage notes.** Annotations and flags are what traders previously concluded about these outages; cite them, and say when the model agrees or disagrees.
- **Generator outages.** IIR outages and derates at plants with meaningful factors; `load_mw` assumes the unit would otherwise have run, so treat it as an upper bound.
- **Small net generator impact.** If the generators move only a few MW on the constraint, the binding is probably driven by outages, load, or flows that are not mapped. Look to the desk notes for outages in effect, and to zonal load and tie flows.
- **Factor sets** are fit from recent binding intervals, so they reflect the outages in place at the time of the fit. Mention it if the fit is old.
- **Desk notes** are what traders believe drove the constraint. Use their outage and generator percentages where the data does not give a number. When the data contradicts a note (for example, a note says a plant was off but its output shows it running), say so plainly.
- **Data quality.** Plant output can differ between sources (muse and gs). If a key plant's number looks wrong (a flat value every hour, or far from the other source), call `get_constraint_drivers` again with `source` `gs` and compare.

</Reading_The_Data>

<Output_Format>

## [Constraint name] ([RT/DA] $[total], peak $[peak] at HE[peak_hr])

**Context:** How today compares with its history (30-day total, largest day, how often it binds).

**Drivers:** The main causes, most important first, each with its number: generator moves with `load_mw`, outages from the notes with their percentages, load or tie flow changes. Use the desk's style: "Muddy Run flipped from pumping to generating (+121 MW on the constraint)".

**Working against it:** Relieving factors, including likely redispatch.

**Confidence:** High, medium, or low, and what data was missing.

End with a short **Unexplained** list: constraints that bound high where the data and notes do not explain why.

</Output_Format>
