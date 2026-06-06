export type ConnectionType = "Management" | "Layer 1 Switch" | "Layer 2 Switch" | "Layer 3 Switch";

export interface DriverPackageInfo {
  id: string;
  name: string;
  description: string | null;
  connection_type: string;
  filename: string;
  size_bytes: number;
  sha256: string;
  uploaded_by: string;
  created_at: string;
  updated_at: string;
}

export interface DriverUpdate {
  name?: string;
  description?: string;
  connection_type?: string;
}
