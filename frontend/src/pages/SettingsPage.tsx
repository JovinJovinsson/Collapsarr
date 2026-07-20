import { PlaceholderView } from "../components/PlaceholderView";
import { SettingsIcon } from "../components/icons";

export function SettingsPage() {
  return (
    <PlaceholderView
      title="Settings"
      summary="Instances, path mappings, downmix targets, Connect, and general options."
      emptyState="Settings aren't editable here yet. Instance and downmix configuration will live on this screen."
      ticket="COL-33"
      icon={<SettingsIcon width={28} height={28} />}
    />
  );
}
