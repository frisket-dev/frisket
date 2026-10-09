import { pdfPacketThumbnailUrl } from '../../../api/pdfPacketSplits';

export function PacketThumbnail({
  projectId,
  splitId,
  page,
  alt = '',
  eager = false,
}: {
  projectId: string;
  splitId: string;
  page: number;
  alt?: string;
  eager?: boolean;
}) {
  return (
    <img
      loading={eager ? 'eager' : 'lazy'}
      src={pdfPacketThumbnailUrl(projectId, splitId, page)}
      alt={alt}
    />
  );
}
