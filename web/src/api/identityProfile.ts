import { httpContract } from './httpContract';
import type { HttpProfilePatchRequest } from '../generated/openHttpContracts';
import type { MeInfo } from './types';

export interface IdentityProfileOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ProfilePatchInput {
  display_name?: string | null;
  cost_preapproval_usd?: string | null;
}

export interface IdentityProfileApi {
  getMe(options?: IdentityProfileOptions): Promise<MeInfo>;
  updateProfile(
    input: ProfilePatchInput,
    options?: IdentityProfileOptions,
  ): Promise<MeInfo>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
export function createIdentityProfileApi(
  errorFactory: ContractErrorFactory,
): IdentityProfileApi {
  return {
    getMe(options = {}) {
      return httpContract(
        'outer.me.get',
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    updateProfile(input, options = {}) {
      return httpContract(
        'outer.update_profile.patch',
        {
          pathParams: {},
          query: {},
          // The live request deliberately ignores edition-owned extra keys;
          // this adapter's public input remains the one shared profile field.
          body: input as HttpProfilePatchRequest,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
