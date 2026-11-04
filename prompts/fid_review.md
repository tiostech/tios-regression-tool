<!--
Instructions for the `fid_review` MCP prompt (see mcp_server.py).

This reviews the latest FID (forward impact decomposition) assessment run for a trader: what
changed since the previous run and the biggest upcoming risks. Edit this file to change what
the report looks like.

Leave {{market}} and {{MARKET}} in place; the server fills them in. This comment is not sent
to the AI.
-->
<Steps>

Review the latest {{MARKET}} FID run for the desk.

1. Call `list_fid_runs` with market `{{market}}`. Note whether the latest scheduled run completed, how many elements it wrote of those requested, how many queries errored, and which elements were added or dropped since the previous run (and why they were picked).
2. Call `get_fid_changes` with market `{{market}}`. If it reports that the results on S3 can't be read, say so plainly, report what step 1 shows, and stop.
3. For the top risks, if a desk note would help, call `get_monitored_element_notes` for the element, or `get_outage_documentation` for a driving outage.
4. Write the report in the Output_Format below.

Use only the data the tools return. If the data does not support a conclusion, say so.

</Steps>

<Reading_The_Data>

- **Loading.** `peak loading` is post-outage flow (the SE case's base flow plus the day's total impact of every outage out that day) as a % of the element's Rating B, in that constraint's contingency. Near or over 100% is the risk. The base case is one fixed state-estimator snapshot, so treat the level as approximate and the change from outages as the signal.
- **Outage impacts.** + loads the constraint, - shields it (relieves it). An outage that shields and is coming back into service raises loading when it returns; say so when its out days end inside the window.
- **Changes.** Comparisons cover only the forward days both runs share. If the SE case changed between the runs, every number can move for that reason alone; say so before attributing a change to outages.
- **Binding history.** The RT shadow total over the last 14 days shows whether the element is already binding. A high forward loading on an element that is not binding yet is an emerging risk; on one that is binding, it says the binding should continue or worsen.
- **Coverage.** Elements written with no result file, and requested elements the run never wrote, are gaps, not "no risk".

</Reading_The_Data>

<Output_Format>

**{{MARKET}} FID review** (run time in Central, SE case, elements written / requested)

**Biggest upcoming risks** — one bullet per element, most loaded first (at most 8): element and constraint (keep the links), peak loading % and day, the 1–3 outages driving it with their impact and out days, and whether it is binding now.

**What changed** — the largest changes since the previous run: loading moves, outages newly mattering or rescheduled, and elements added or dropped from the run (with the reason they were picked).

**Gaps** — failed or incomplete runs, errors, elements with no results.

Keep it short; a trader should read it in a minute.

</Output_Format>
