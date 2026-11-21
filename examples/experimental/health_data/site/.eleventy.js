module.exports = function (config) {
  config.addPassthroughCopy("plot.svg");
  return {dir: {input: ".", output: "_site", includes: "_includes"}};
};
