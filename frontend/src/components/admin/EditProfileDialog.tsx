import toast from "react-hot-toast";
import { useUpdateUserProfile } from "@/api/admin";
import { Modal } from "@/components/ui/Modal";
import { ProfileForm } from "@/components/profile/ProfileForm";
import { profileSaveErrorText } from "@/lib/errors";
import type { ProfileUpdate, User } from "@/types/auth.types";

interface EditProfileDialogProps {
  user: User | null;
  onClose: () => void;
}

/**
 * The Users page Edit profile dialog (issue #1252): an admin or superadmin
 * edits any account's name, contact email, and phone through
 * PATCH /auth/users/{id}/profile. Built on the shared Modal, so it is the same
 * centred native dialog as every other modal (Modal.tsx carries m-auto).
 */
export function EditProfileDialog({ user, onClose }: EditProfileDialogProps) {
  const update = useUpdateUserProfile();

  const handleSave = async (profile: ProfileUpdate) => {
    if (!user) return;
    try {
      await update.mutateAsync({ userId: user.id, profile });
      toast.success("Profile saved");
      onClose();
    } catch (err) {
      toast.error(profileSaveErrorText(err, "Failed to save profile"));
    }
  };

  return (
    <Modal open={user !== null} onClose={onClose} title="Edit profile">
      {user && (
        <ProfileForm
          key={user.id}
          user={user}
          idPrefix="edit-profile"
          isPending={update.isPending}
          onSave={handleSave}
          onCancel={onClose}
        />
      )}
    </Modal>
  );
}
