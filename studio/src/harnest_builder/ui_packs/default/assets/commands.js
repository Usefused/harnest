/** Render copyable POSIX argv without treating spaces or shell syntax as code. */
export function commandText(argv) {
  return argv.map(value=>/^[a-zA-Z0-9_./:@=+-]+$/.test(value)?value:"'"+value.replaceAll("'","'\"'\"'")+"'").join(" ");
}

/** Mirror the server-owned CLI prefix and company initialization options. */
export function initCommand(cli, {directory,name,framework,mode,profile}) {
  const args=[...(cli?.command||["harnest"]),"init",`${directory.replace(/\/$/,"")}/${name||"my-agent"}`,"--framework",framework,"--mode",mode];
  if(profile!=="guided")args.push(`--${profile}`);
  return commandText([...args,...(cli?.init_args||[])]);
}

/** Preserve company and multi-argument launcher identities in job summaries. */
export function jobCommand(job) {
  return commandText([...(job.command||job.argv.slice(0,1)),...(job.args||job.argv.slice(1)).slice(0,2)]);
}
