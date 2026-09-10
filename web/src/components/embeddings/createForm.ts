export interface CreateEmbeddingIndexForm {
  provider: string;
  model: string;
  sourceColumns: string[];
  allowRemote: boolean;
  allowRemoteAutomaticRefresh: boolean;
  maxCost: string;
  confirmRemote: boolean;
}

export const emptyCreateEmbeddingIndexForm = (
  firstColumn: string | undefined,
): CreateEmbeddingIndexForm => ({
  provider: '',
  model: '',
  sourceColumns: firstColumn ? [firstColumn] : [],
  allowRemote: false,
  allowRemoteAutomaticRefresh: false,
  maxCost: '',
  confirmRemote: false,
});
