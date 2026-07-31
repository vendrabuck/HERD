// Metadata only; the secrets service never returns plaintext through this
// shape (see services/secrets/app/schemas/secret.py SecretResponse). The
// frontend has no secret-authoring surface yet: this type backs a read-only
// picker (e.g. the hypervisor registration form's secret_id selector), not a
// secrets management page.
export interface SecretSummary {
  id: string;
  name: string;
  type: string;
  description: string | null;
  key_version: number;
  created_at: string;
  updated_at: string;
}
