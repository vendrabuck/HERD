import type { ProfileUpdate, User } from "@/types/auth.types";

// The editable profile fields as the form holds them (issue #1252): plain
// strings, an empty string meaning "not set" for the two optional fields.
export interface ProfileFormFields {
  full_name: string;
  contact_email: string;
  phone: string;
}

// Column widths the backend enforces (services/auth/app/schemas/auth.py); the
// inputs cap at the same lengths. The phone character rule stays server-side
// and comes back as a 422 the form reports per field.
export const PHONE_MAX_LENGTH = 32;
export const FULL_NAME_MAX_LENGTH = 200;

export function profileFormFields(user: User): ProfileFormFields {
  return {
    full_name: user.full_name ?? "",
    contact_email: user.contact_email ?? "",
    phone: user.phone ?? "",
  };
}

function optional(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/**
 * The profile PATCH to send: only the fields that differ from `baseline` (the
 * account as it stood when the form opened), compared after trimming, so a
 * field the user left alone is never sent and a value another writer stored
 * meanwhile is not overwritten with the snapshot. A blank optional field is
 * sent as null, which clears it. An empty object means there is nothing to
 * save.
 */
export function profileEditPayload(
  baseline: ProfileFormFields,
  form: ProfileFormFields,
): ProfileUpdate {
  const out: ProfileUpdate = {};
  const name = form.full_name.trim();
  if (name !== baseline.full_name.trim()) out.full_name = name;
  const contact = optional(form.contact_email);
  if (contact !== optional(baseline.contact_email)) out.contact_email = contact;
  const phone = optional(form.phone);
  if (phone !== optional(baseline.phone)) out.phone = phone;
  return out;
}

/** The name the header and tables show: full_name, else the username. */
export function displayName(user: Pick<User, "full_name" | "username">): string {
  const name = user.full_name?.trim();
  return name ? name : user.username;
}
