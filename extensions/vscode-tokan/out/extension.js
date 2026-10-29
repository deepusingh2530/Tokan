exports.activate = function(context) {
  console.log('Tokan Enterprise Gateway extension activated');
  return { dispose: function() {} };
};
exports.deactivate = function() {};
