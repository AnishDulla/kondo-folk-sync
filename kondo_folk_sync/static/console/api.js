export function createConsoleApi(config) {
  const tokenQuery = () => config.token ? `?token=${encodeURIComponent(config.token)}` : "";

  async function getState() {
    const response = await fetch(`${config.adminPrefix}/console-state${tokenQuery()}`);
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return response.json();
  }

  async function post(path, body = null) {
    const response = await fetch(`${config.adminPrefix}${path}${tokenQuery()}`, {
      method: "POST",
      body,
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return response.json();
  }

  return { getState, post };
}
