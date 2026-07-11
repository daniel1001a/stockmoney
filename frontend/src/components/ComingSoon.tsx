interface Props {
  title: string
  body: string
}

// Honest empty-state box for features that depend on a backend that doesn't
// exist yet (e.g. the trader-league arena). Never fetches anything -- a
// missing endpoint would 404 and read as "broken," not "not built yet."
export default function ComingSoon({ title, body }: Props) {
  return (
    <div className="rounded-lg border border-dashed border-neutral-800 bg-neutral-900/50 p-4">
      <h2 className="text-sm font-medium text-neutral-300 mb-1">{title}</h2>
      <p className="text-neutral-600 italic text-sm">{body}</p>
    </div>
  )
}
