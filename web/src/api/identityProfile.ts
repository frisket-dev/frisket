import { httpContract, type RequestOptions } from './httpContract';
import type { HttpProfilePatchRequest } from '../generated/openHttpContracts';
import type { MeInfo } from './types';

interface ProfilePatchInput {
  display_name?: string | null;
  cost_preapproval_usd?: string | null;
}

interface IdentityProfileApi {
  getMe(options?: RequestOptions): Promise<MeInfo>;
  updateProfile(
    input: ProfilePatchInput,
    options?: RequestOptions,
  ): Promise<MeInfo>;
}

export function createIdentityProfileApi(
): IdentityProfileApi {
  return {
    getMe(options = {}) {
      return httpContract(
        'outer.me.get',
        {
          signal: options.signal,
        },
      );
    },

    updateProfile(input, options = {}) {
      return httpContract(
        'outer.update_profile.patch',
        {
          // The live request deliberately ignores edition-owned extra keys;
          // this adapter's public input remains the one shared profile field.
          body: input as HttpProfilePatchRequest,
          signal: options.signal,
        },
      );
    },
  };
}
