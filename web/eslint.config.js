import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'

export default tseslint.config(
  // tests/golden holds generated fixtures; dist is build output.
  { ignores: ['dist', 'tests/golden'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // A leading underscore marks a parameter bound deliberately and not
      // read -- the `never` binding that makes a switch exhaustive being the
      // case that matters here, since its whole job is to fail compilation
      // rather than to be used. Only the parameter form is allowed: an
      // unused local or caught error is a mistake, not a signature.
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },
)
