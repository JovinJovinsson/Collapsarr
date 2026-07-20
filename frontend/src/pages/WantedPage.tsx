import { PlaceholderView } from "../components/PlaceholderView";
import { WantedIcon } from "../components/icons";

export function WantedPage() {
  return (
    <PlaceholderView
      title="Wanted"
      summary="Monitored files still missing an enabled downmix target."
      emptyState="Nothing to show yet. Once instances are connected, files needing a downmix will appear here."
      ticket="COL-31"
      icon={<WantedIcon width={28} height={28} />}
    />
  );
}
