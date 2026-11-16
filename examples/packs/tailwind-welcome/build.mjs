import {execFileSync} from 'node:child_process';
import {mkdtemp, readFile, writeFile, mkdir, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

/** Embed compiled CSS because isolated Studio frames cannot load external stylesheets. */
async function build() {
  const root=fileURLToPath(new URL('.',import.meta.url));
  const temporary=await mkdtemp(join(tmpdir(),'harnest-tailwind-'));
  try {
    const output=join(temporary,'tailwind.css');
    const cli=join(root,'node_modules/@tailwindcss/cli/dist/index.mjs');
    execFileSync(process.execPath,[cli,'-i','src/input.css','-o',output,'--minify'],{cwd:root,stdio:'inherit'});
    const template=await readFile(join(root,'src/welcome.html'),'utf8');
    const marker='/* STUDIO_TAILWIND_CSS */';
    if(template.split(marker).length!==2)throw new Error('Expected exactly one CSS insertion marker');
    const css=await readFile(output,'utf8');
    const html=template.replace(marker,()=>css);
    const asset=join(root,'assets/welcome.html');
    if(process.argv.includes('--check')) {
      if(await readFile(asset,'utf8')!==html)throw new Error('Generated welcome.html is stale; run npm run build');
    } else {
      await mkdir(join(root,'assets'),{recursive:true});
      await writeFile(asset,html);
    }
  } finally {
    await rm(temporary,{recursive:true,force:true});
  }
}

await build();
