// Copyright 2017 The Oppia Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS-IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/**
 * @fileoverview Directive for fraction editor.
 */

angular.module('oppia').directive('fractionEditor', [
  'FractionObjectFactory', 'UrlInterpolationService',
  function(
      FractionObjectFactory, UrlInterpolationService) {
    return {
      restrict: 'E',
      scope: {},
      bindToController: {
        value: '='
      },
      templateUrl: UrlInterpolationService.getExtensionResourceUrl(
        '/objects/templates/fraction-editor.directive.html'),
      controllerAs: '$ctrl',
      controller: ['$scope', function($scope) {
        var ctrl = this;
        var errorMessage = '';
        var fractionString = '0';
        if (ctrl.value !== null) {
          var defaultFraction = FractionObjectFactory.fromDict(ctrl.value);
          fractionString = defaultFraction.toString();
        }
        ctrl.localValue = {
          label: fractionString
        };

        $scope.$watch('$ctrl.localValue.label', function(newValue) {
          try {
            var INTERMEDIATE_REGEX = /^\s*-?\s*$/;
            if (!INTERMEDIATE_REGEX.test(newValue)) {
              ctrl.value = FractionObjectFactory.fromRawInputString(newValue);
            }
            errorMessage = '';
          } catch (parsingError) {
            errorMessage = parsingError.message;
          }
        });

        ctrl.getWarningText = function() {
          return errorMessage;
        };
      }]
    };
  }]);
