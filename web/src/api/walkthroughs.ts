import { httpContract, type HttpContractSuccessResponse } from './httpContract';

type WalkthroughCatalogWire =
  HttpContractSuccessResponse<'tenant.list_walkthroughs.get'>;

export function listWalkthroughs(): Promise<{
  walkthroughs: readonly { id: string; badges: readonly string[] }[];
}> {
  return httpContract(
    'tenant.list_walkthroughs.get',
    { pathParams: {}, query: {} },
    (wire: WalkthroughCatalogWire) => ({
      walkthroughs: wire.walkthroughs.map((walkthrough) => ({
        id: walkthrough.id,
        badges: walkthrough.badges,
      })),
    }),
  );
}
