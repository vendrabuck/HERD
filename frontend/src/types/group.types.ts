export interface GroupMember {
  user_id: string;
  username: string;
  email: string;
  added_at: string;
}

export interface Group {
  id: string;
  name: string;
  description: string | null;
  created_by: string | null;
  created_at: string;
}

export interface GroupDetail extends Group {
  members: GroupMember[];
}

export interface GroupCreate {
  name: string;
  description?: string | null;
}

export interface GroupUpdate {
  name?: string;
  description?: string | null;
}
