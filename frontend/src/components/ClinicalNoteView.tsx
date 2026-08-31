import type { components } from '../api/schema'

type ClinicalNote = components['schemas']['ClinicalNote']
type FieldValue = components['schemas']['FieldValue']

// protojson omits an unset field entirely rather than emitting `null`
// (openapi/coda-v1.yaml's ClinicalNote schema description) — so "not
// stated" is `field === undefined`, not `field === null`. Never render a
// guess in its place (claude_context.md §3).
function ScalarField({ label, field }: { label: string; field: FieldValue | undefined }) {
  return (
    <div style={{ marginBottom: '0.75rem' }}>
      <dt style={{ fontWeight: 'bold' }}>{label}</dt>
      <dd style={{ margin: 0 }}>
        {field?.value ? (
          <>
            {field.value}
            {field.source_turn_ids && field.source_turn_ids.length > 0 && (
              <span style={{ color: '#888', fontSize: '0.85em' }}>
                {' '}
                (turns {field.source_turn_ids.join(', ')})
              </span>
            )}
          </>
        ) : (
          <em style={{ color: '#888' }}>Not stated</em>
        )}
      </dd>
    </div>
  )
}

function ListField({ label, items }: { label: string; items: FieldValue[] | undefined }) {
  return (
    <div style={{ marginBottom: '0.75rem' }}>
      <dt style={{ fontWeight: 'bold' }}>{label}</dt>
      <dd style={{ margin: 0 }}>
        {items && items.length > 0 ? (
          <ul style={{ margin: 0, paddingLeft: '1.25rem' }}>
            {items.map((item, i) => (
              <li key={i}>
                {item.value}
                {item.source_turn_ids && item.source_turn_ids.length > 0 && (
                  <span style={{ color: '#888', fontSize: '0.85em' }}>
                    {' '}
                    (turns {item.source_turn_ids.join(', ')})
                  </span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <em style={{ color: '#888' }}>None stated</em>
        )}
      </dd>
    </div>
  )
}

export function ClinicalNoteView({ note }: { note: ClinicalNote }) {
  return (
    <dl>
      <ScalarField label="Chief Complaint" field={note.chief_complaint} />
      <ScalarField label="History of Present Illness" field={note.hopi} />
      <ListField label="Past Medical History" items={note.past_medical_history} />
      <ListField label="Medications" items={note.medications_allergies?.medications} />
      <ListField label="Allergies" items={note.medications_allergies?.allergies} />
      <ScalarField label="Examination Findings" field={note.examination_findings} />
      <ListField label="Provisional Diagnosis" items={note.provisional_diagnosis} />
      <ListField label="Investigations Advised" items={note.investigations_advised} />
      <ScalarField label="Treatment Plan & Advice" field={note.treatment_plan} />
    </dl>
  )
}
