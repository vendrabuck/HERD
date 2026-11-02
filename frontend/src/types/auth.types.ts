export interface User {
  id: string;
  email: string;
  username: string;
  // Profile fields (issue #1252). full_name is the display name, never empty;
  // contact_email, when set, is where email notifications go instead of the
  // sign-in email; phone is stored and displayed only.
  full_name: string;
  contact_email: string | null;
  phone: string | null;
  is_active: boolean;
  role: string;
  created_at: string;
  updated_at?: string;
  modified_by?: string | null;
}

// Body of PATCH /auth/me/profile and PATCH /auth/users/{id}/profile. An
// omitted key is unchanged; null (or an empty string) clears contact_email or
// phone; full_name can never be cleared.
export interface ProfileUpdate {
  full_name?: string;
  contact_email?: string | null;
  phone?: string | null;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface RegisterRequest {
  email: string;
  username: string;
  password: string;
  // Optional (issue #1252): omitted stores the username as the name.
  full_name?: string;
}
