
---

Your previous response was rejected by the validator.

Validation error:
{validation_error}

Your previous response:
{previous_response}

Correct the specific problem named above and output the corrected JSON
object. Common causes: citing a `source_thought_id` that was not in the
supporting thoughts for that field, putting content in `value` for a
list-valued field (or in `items` for a scalar one), returning a `fields`
object whose keys do not exactly match the ones requested, giving content
without citing any source thought ids, or wrapping the JSON in markdown code
fences.

Output only the corrected JSON object. No prose.
