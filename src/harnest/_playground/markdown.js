"use strict";

/** Render untrusted agent Markdown without enabling HTML or remote image loads. */
const harnestMarkdown = (() => {
  const parser = markdownit({ html: false, breaks: true, linkify: true });
  // A model must not turn the authenticated playground into an image-fetch proxy.
  // Keep image descriptions visible, but never create an automatic request.
  parser.disable("image");
  parser.validateLink = (url) => /^(https?:\/\/|mailto:)/i.test(url);
  parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    tokens[index].attrSet("target", "_blank");
    tokens[index].attrSet("rel", "noopener noreferrer");
    return renderer.renderToken(tokens, index, options);
  };
  // Alignment styles would violate the playground's self-only style policy.
  for (const name of ["th_open", "td_open"]) {
    parser.renderer.rules[name] = (tokens, index, options, env, renderer) => {
      const alignment = tokens[index].attrGet("style")?.replace("text-align:", "");
      tokens[index].attrs = alignment ? [["class", `markdown-align-${alignment}`]] : [];
      return renderer.renderToken(tokens, index, options);
    };
  }
  return Object.freeze({ render: (source) => parser.render(source) });
})();
