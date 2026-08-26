/**
 * Single source of truth for "is this user an admin". Previously hand-copied
 * as `user?.role === "admin" || user?.role === "superadmin"` at every call
 * site that gated an admin-only page or nav link (issue #561), which risked
 * a role being added or renamed in only some of them. Case-sensitive: only
 * the literal lowercase "admin"/"superadmin" role strings count.
 */
export function isAdminRole(role: string | undefined | null): boolean {
  return role === "admin" || role === "superadmin";
}
