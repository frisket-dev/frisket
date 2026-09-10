// Fixture ONLY: proves the real import.meta.glob + collectEagerRegistrations
// collision path end to end (core/registration/glob.test.ts). Two files in
// this directory deliberately claim the same key.
export default { key: 'dupFixture', from: 'a' };
