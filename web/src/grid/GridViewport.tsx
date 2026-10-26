import { forwardRef, useMemo } from 'react';
import {
  DataEditor,
  type DataEditorProps,
  type DataEditorRef,
} from '@glideapps/glide-data-grid';
import '@glideapps/glide-data-grid/dist/index.css';

import { customRenderers, headerIcons } from './cells';

type SharedViewportProp = keyof Pick<DataEditorProps,
  'width' | 'height' | 'customRenderers' | 'headerIcons' | 'rowMarkers'
  | 'headerHeight' | 'groupHeaderHeight' | 'smoothScrollX' | 'smoothScrollY'
  | 'verticalBorder'>;

/** The common canvas viewport used by the sheet and read-only projections.
 * Consumers own data and interactions; the renderer and grid geometry stay
 * identical. */
export type GridViewportProps = Omit<DataEditorProps, SharedViewportProp> & {
  extraHeaderIcons?: DataEditorProps['headerIcons'];
};

export const GridViewport = forwardRef<DataEditorRef, GridViewportProps>(function GridViewport(
  { extraHeaderIcons, ...props },
  ref,
) {
  const icons = useMemo(() => extraHeaderIcons
    ? { ...headerIcons, ...extraHeaderIcons } : headerIcons, [extraHeaderIcons]);
  return <DataEditor
    {...props}
    ref={ref}
    width="100%"
    height="100%"
    customRenderers={customRenderers}
    headerIcons={icons}
    rowMarkers={{ kind: 'both', width: 40 }}
    headerHeight={34}
    groupHeaderHeight={28}
    smoothScrollX
    smoothScrollY
    verticalBorder={false}
  />;
});
