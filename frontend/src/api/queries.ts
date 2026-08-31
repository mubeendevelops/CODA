import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from './client'
import { unwrap } from './helpers'
import type { components } from './schema'

type Consultation = components['schemas']['Consultation']
type CreateConsultationRequest = components['schemas']['CreateConsultationRequest']
type Job = components['schemas']['Job']
type ConsultationResult = components['schemas']['ConsultationResult']
type TranscriptPresignResponse = components['schemas']['TranscriptPresignResponse']

export function useConsultations() {
  return useQuery({
    queryKey: ['consultations'],
    queryFn: async (): Promise<Consultation[]> => {
      const result = await api.GET('/consultations', {})
      return unwrap(result, 'Failed to load consultations')
    },
  })
}

export function useCreateConsultation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body: CreateConsultationRequest): Promise<Consultation> => {
      const result = await api.POST('/consultations', { body })
      return unwrap(result, 'Failed to create consultation')
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['consultations'] })
    },
  })
}

export function useJob(jobId: string | undefined) {
  return useQuery({
    queryKey: ['jobs', jobId],
    enabled: jobId !== undefined,
    queryFn: async (): Promise<Job> => {
      const result = await api.GET('/jobs/{id}', { params: { path: { id: jobId! } } })
      return unwrap(result, 'Failed to load job')
    },
  })
}

export function useConsultationResult(consultationId: string | undefined) {
  return useQuery({
    queryKey: ['consultations', consultationId, 'result'],
    enabled: consultationId !== undefined,
    retry: false, // a 409 (still processing) is an expected, not a transient, failure
    queryFn: async (): Promise<ConsultationResult> => {
      const result = await api.GET('/consultations/{id}/result', {
        params: { path: { id: consultationId! } },
      })
      return unwrap(result, 'Result is not ready yet')
    },
  })
}

export function useTranscriptPresign(consultationId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ['consultations', consultationId, 'transcript-presign'],
    enabled: enabled && consultationId !== undefined,
    retry: false,
    queryFn: async (): Promise<TranscriptPresignResponse> => {
      const result = await api.GET('/consultations/{id}/transcript', {
        params: { path: { id: consultationId! } },
      })
      return unwrap(result, 'Transcript is not ready yet')
    },
  })
}
