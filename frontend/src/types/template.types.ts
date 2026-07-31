export type FieldType = "string" | "number" | "boolean" | "dropdown" | "password";

export interface FieldDefinition {
  key: string;
  label: string;
  type: FieldType;
  required?: boolean;
  default?: string | number | boolean | null;
  options?: string[];
}

export interface SectionDefinition {
  name: string;
  fields: FieldDefinition[];
}

export interface DeviceTemplate {
  id: string;
  name: string;
  template_type: string;
  driver_id: string | null;
  driver_name: string | null;
  connection_type: string | null;
  hypervisor_id: string | null;
  exclusive: boolean;
  icon: string | null;
  description: string | null;
  vendor: string;
  model: string;
  part_number: string | null;
  sections: SectionDefinition[];
  created_at: string;
  updated_at: string;
  poll_interval_seconds: number | null;
}

export interface TemplateCreate {
  name: string;
  template_type?: "device" | "port" | "dynamic";
  driver_id?: string | null;
  hypervisor_id?: string | null;
  exclusive?: boolean;
  icon?: string | null;
  description?: string;
  vendor?: string;
  model?: string;
  part_number?: string | null;
  sections: SectionDefinition[];
  poll_interval_seconds?: number | null;
}

export interface TemplateUpdate {
  name?: string;
  driver_id?: string | null;
  hypervisor_id?: string | null;
  exclusive?: boolean;
  icon?: string | null;
  description?: string;
  vendor?: string;
  model?: string;
  part_number?: string | null;
  sections?: SectionDefinition[];
  poll_interval_seconds?: number | null;
}
