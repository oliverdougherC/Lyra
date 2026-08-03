'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { ClassCreate, ClassUpdate } from '@/types'

export const classKeys = {
  all: ['classes'] as const,
  detail: (classId: number) => ['class', classId] as const,
}

export function useClasses() {
  return useQuery({
    queryKey: classKeys.all,
    queryFn: ({ signal }) => api.listClasses(signal),
  })
}

export function useClass(classId: number) {
  return useQuery({
    queryKey: classKeys.detail(classId),
    queryFn: ({ signal }) => api.getClass(classId, signal),
    enabled: Number.isFinite(classId),
  })
}

export function useCreateClass() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: ClassCreate) => api.createClass(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: classKeys.all }),
  })
}

export function useUpdateClass() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ classId, body }: { classId: number; body: ClassUpdate }) =>
      api.updateClass(classId, body),
    onSuccess: (updated) => {
      queryClient.invalidateQueries({ queryKey: classKeys.all })
      queryClient.setQueryData(classKeys.detail(updated.id), updated)
    },
  })
}

export function useDeleteClass() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (classId: number) => api.deleteClass(classId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: classKeys.all }),
  })
}
