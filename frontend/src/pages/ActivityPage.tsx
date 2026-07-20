import { PlaceholderView } from "../components/PlaceholderView";
import { ActivityIcon } from "../components/icons";

export function ActivityPage() {
  return (
    <PlaceholderView
      title="Activity"
      summary="History of downmix jobs — queued, running, completed, and failed."
      emptyState="No activity yet. Downmix jobs and their history will be listed here as they run."
      ticket="COL-32"
      icon={<ActivityIcon width={28} height={28} />}
    />
  );
}
