"use client"

/** Ends the session on the server, then goes to the sign-in page. */
export function SignOutButton({ className }: { className?: string }) {
  async function signOut() {
    await fetch("/session/logout", { method: "POST" }).catch(() => undefined)
    window.location.replace("/login")
  }
  return (
    <button onClick={signOut} className={className}>
      Sign out
    </button>
  )
}
