#!/usr/bin/env node
'use strict';
// `npx -y health-data-registry`: start the MCP server through uv. See ../lib/launcher.js.
require('../lib/launcher')
  .main()
  .then((code) => {
    process.exitCode = code;
  });
