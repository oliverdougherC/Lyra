'use client'

import { useEffect } from 'react'
import { zodResolver } from '@hookform/resolvers/zod'
import { useForm, useWatch } from 'react-hook-form'
import { toast } from 'sonner'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { useCreateClass, useUpdateClass } from '@/lib/hooks/use-classes'
import type { ClassRead } from '@/types'

const schema = z.object({
  name: z
    .string()
    .trim()
    .min(1, 'Give the class a name.')
    .max(120, 'Keep the name under 120 characters.'),
  code: z.string().trim().max(40, 'Keep the code under 40 characters.'),
  semester: z.string().trim().max(40, 'Keep the term under 40 characters.'),
})

type FormValues = z.infer<typeof schema>

/** Recent terms, offered as suggestions rather than a closed list. */
function recentTerms(now = new Date()): string[] {
  const year = now.getFullYear()
  const terms: string[] = []
  for (const offset of [0, -1]) {
    for (const season of ['Fall', 'Summer', 'Spring']) terms.push(`${season} ${year + offset}`)
  }
  return terms.slice(0, 5)
}

type ClassFormDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present when editing, absent when creating. */
  klass?: ClassRead | null
  onCreated?: (klass: ClassRead) => void
}

export function ClassFormDialog({ open, onOpenChange, klass, onCreated }: ClassFormDialogProps) {
  const isEdit = Boolean(klass)
  const createClass = useCreateClass()
  const updateClass = useUpdateClass()
  const pending = createClass.isPending || updateClass.isPending

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    mode: 'onSubmit',
    defaultValues: { name: '', code: '', semester: '' },
  })
  const { reset, control, register, setError, handleSubmit, formState } = form

  useEffect(() => {
    if (!open) return
    reset({ name: klass?.name ?? '', code: klass?.code ?? '', semester: klass?.semester ?? '' })
  }, [open, klass, reset])

  const nameValue = useWatch({ control, name: 'name' })
  const terms = recentTerms()

  async function onSubmit(values: FormValues) {
    const body = {
      name: values.name.trim(),
      code: values.code.trim() || null,
      semester: values.semester.trim() || null,
    }
    try {
      if (klass) {
        await updateClass.mutateAsync({ classId: klass.id, body })
        toast.success('Class updated.')
      } else {
        const created = await createClass.mutateAsync(body)
        toast.success(`${created.name} created.`)
        onCreated?.(created)
      }
      onOpenChange(false)
    } catch (caught) {
      const message =
        caught instanceof ApiError
          ? caught.message
          : 'Could not save this class. Check the details and try again.'
      // A duplicate name is a field problem, so it belongs on the field rather than a toast.
      setError(caught instanceof ApiError && caught.status === 409 ? 'name' : 'root', { message })
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Rename class' : 'New class'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'Update how this class appears in your list.'
              : 'Name it now. You can add documents right after.'}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            <Field data-invalid={formState.errors.name ? true : undefined}>
              <FieldLabel htmlFor="class-name">Name</FieldLabel>
              <Input
                id="class-name"
                autoComplete="off"
                placeholder="Multivariable Calculus"
                aria-invalid={Boolean(formState.errors.name)}
                {...register('name')}
              />
              <FieldError errors={[formState.errors.name]} />
            </Field>

            <Field data-invalid={formState.errors.code ? true : undefined}>
              <FieldLabel htmlFor="class-code">Course code</FieldLabel>
              <Input
                id="class-code"
                autoComplete="off"
                placeholder="MATH 201"
                aria-invalid={Boolean(formState.errors.code)}
                {...register('code')}
              />
              <FieldDescription>Optional. Used for the card initials.</FieldDescription>
              <FieldError errors={[formState.errors.code]} />
            </Field>

            <Field data-invalid={formState.errors.semester ? true : undefined}>
              <FieldLabel htmlFor="class-semester">Semester</FieldLabel>
              <Input
                id="class-semester"
                list="class-semester-options"
                autoComplete="off"
                placeholder={terms[0]}
                aria-invalid={Boolean(formState.errors.semester)}
                {...register('semester')}
              />
              <datalist id="class-semester-options">
                {terms.map((term) => (
                  <option key={term} value={term} />
                ))}
              </datalist>
              <FieldDescription>Optional. Pick a recent term or type your own.</FieldDescription>
              <FieldError errors={[formState.errors.semester]} />
            </Field>

            {formState.errors.root ? (
              <p className="text-danger-text text-sm" role="alert">
                {formState.errors.root.message}
              </p>
            ) : null}
          </FieldGroup>

          <DialogFooter className="mt-6">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={pending || nameValue.trim().length === 0}>
              {pending ? <Spinner /> : null}
              {isEdit ? 'Save' : 'Create class'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
