import {
  AnthropicVendorIcon,
  DeepSeekVendorIcon,
  GoogleVendorIcon,
  GroqVendorIcon,
  MistralVendorIcon,
  OpenAiVendorIcon,
  OpenRouterVendorIcon,
} from './components/icons.jsx'

// Shared between LlmConfigsPage (managing configs) and the Runs pages
// (showing which one a run used) -- one place to keep vendor display
// labels/icons in sync with core/llm_config.py's vendor keys.
export const VENDOR_LABELS = {
  google: 'Google',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  mistral: 'Mistral',
  groq: 'Groq',
  deepseek: 'DeepSeek',
  openrouter: 'OpenRouter',
}
export const VENDOR_ICONS = {
  google: GoogleVendorIcon,
  openai: OpenAiVendorIcon,
  anthropic: AnthropicVendorIcon,
  mistral: MistralVendorIcon,
  groq: GroqVendorIcon,
  deepseek: DeepSeekVendorIcon,
  openrouter: OpenRouterVendorIcon,
}

// vendor/model as recorded on a run (or a config) -- `unknown` shows for
// older runs created before llm_vendor/llm_model were tracked.
export function VendorBadge({ vendor, model, showModel = false }) {
  if (!vendor) return '—'
  const VendorIcon = VENDOR_ICONS[vendor]
  return (
    <span className="vendor-label" title={showModel ? undefined : model}>
      {VendorIcon && <VendorIcon size={16} />}
      {VENDOR_LABELS[vendor] || vendor}
      {showModel && model ? ` — ${model}` : ''}
    </span>
  )
}
