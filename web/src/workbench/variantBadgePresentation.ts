import type { SpriteMap } from '@glideapps/glide-data-grid';

const VARIANT_KEYS = ['A', 'B', 'C', 'D'] as const;

export const VARIANT_BADGE_PRESENTATION = {
  size: 20,
  radius: 4,
  fontSize: 10,
  fontWeight: 600,
  textBaselineY: 14,
} as const;

function badgeSprite(label: string): SpriteMap[string] {
  const { size, radius, fontSize, fontWeight, textBaselineY } = VARIANT_BADGE_PRESENTATION;
  return ({ fgColor, bgColor }) => `
    <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" xmlns="http://www.w3.org/2000/svg">
      <rect x="0.5" y="0.5" width="${size - 1}" height="${size - 1}" rx="${radius}"
        fill="${bgColor}" stroke="${fgColor}" stroke-opacity="0.45"/>
      <text x="${size / 2}" y="${textBaselineY}" text-anchor="middle"
        font-family="IBM Plex Mono, monospace" font-size="${fontSize}"
        font-weight="${fontWeight}" fill="${fgColor}">${label}</text>
    </svg>`;
}

export const variantBadgeHeaderIcons: SpriteMap = {
  variantBadgeA: badgeSprite('A'),
  variantBadgeB: badgeSprite('B'),
  variantBadgeC: badgeSprite('C'),
  variantBadgeD: badgeSprite('D'),
};

export function variantBadgeIconName(label: string): string | undefined {
  const key = label.trim().toUpperCase();
  return VARIANT_KEYS.includes(key as (typeof VARIANT_KEYS)[number])
    ? `variantBadge${key}`
    : undefined;
}
