import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { useCreateConsultation } from '../api/queries'
import { ErrorMessage } from '../components/QueryState'

export function NewConsultationPage() {
  const navigate = useNavigate()
  const createConsultation = useCreateConsultation()
  const [consentObtained, setConsentObtained] = useState(false)
  const [consentType, setConsentType] = useState('verbal')

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!consentObtained) return // belt-and-braces: the submit button is already disabled without it

    const consultation = await createConsultation.mutateAsync({
      language: 'en',
      consent: { consent_obtained: consentObtained, consent_type: consentType },
    })
    if (consultation.id) {
      navigate(`/consultations/${consultation.id}/upload`)
    }
  }

  return (
    <div style={{ maxWidth: 480 }}>
      <h1>New consultation</h1>
      <form onSubmit={handleSubmit}>
        <div>
          <label htmlFor="consent-type">Consent method</label>
          <br />
          <select
            id="consent-type"
            value={consentType}
            onChange={(e) => setConsentType(e.target.value)}
          >
            <option value="verbal">Verbal</option>
            <option value="written">Written</option>
          </select>
        </div>

        <div style={{ marginTop: '1rem' }}>
          <label>
            <input
              type="checkbox"
              checked={consentObtained}
              onChange={(e) => setConsentObtained(e.target.checked)}
            />{' '}
            The patient has given consent for this consultation to be recorded and processed.
            Required — v1 is English-only, and no upload is possible until this is checked.
          </label>
        </div>

        {createConsultation.isError && (
          <div style={{ marginTop: '0.5rem' }}>
            <ErrorMessage
              message={
                createConsultation.error instanceof Error
                  ? createConsultation.error.message
                  : 'Failed to create consultation'
              }
            />
          </div>
        )}

        <button
          type="submit"
          disabled={!consentObtained || createConsultation.isPending}
          style={{ marginTop: '1rem' }}
        >
          {createConsultation.isPending ? 'Creating…' : 'Create and continue to upload'}
        </button>
      </form>
    </div>
  )
}
